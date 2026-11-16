"""Safe file-level storage for Markdown/YAML document collections."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Callable

import yaml

from ..storage import sync_directory
from .files import _inventory, parse_document, read_document, scan_markdown
from .reconcile import SourceSnapshot

MAX_DOCUMENT_BYTES = 524288


def validate_relative_markdown_path(relative: str) -> PurePosixPath:
    """Validate and normalize a relative POSIX path ending in .md without hidden/traversal segments."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or relative.startswith("/"):
        raise ValueError("Expected a relative Markdown path")
    value = PurePosixPath(relative)
    if value.is_absolute() or any(not p or p in {".", ".."} or p.startswith(".") for p in value.parts):
        raise ValueError("Invalid document path")
    if value.suffix != ".md":
        raise ValueError("Document path must end in .md")
    return value


def ensure_no_symlinks(path: Path, stop_at: Path) -> None:
    """Reject symlinks along path up to (but not including) stop_at."""
    for candidate in (path, *path.parents):
        if candidate == stop_at:
            break
        if candidate.is_symlink():
            raise ValueError("Document paths cannot contain symlinks")


def render_markdown_document(metadata: dict[str, Any], content: str) -> bytes:
    """Render YAML front matter and Markdown body into UTF-8 bytes."""
    if not isinstance(metadata, dict) or not isinstance(content, str):
        raise ValueError("Metadata must be a mapping and content must be text")
    json.dumps(metadata, allow_nan=False)
    for field in ("id", "type"):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError(f"Document requires a text {field}")
    rendered = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + content
    raw = rendered.encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds 512 KiB")
    return raw


class DocumentFileStore:
    """Low-level file-authoritative read/scan/save/delete for one collection root."""

    def __init__(self, layout, *, module: str, collection: str, root: Path, boundary_root: Path | None = None):
        self.layout = layout
        self.module = module
        self.collection = collection
        self.root = Path(root)
        self.boundary_root = Path(boundary_root) if boundary_root is not None else self.layout.root

    def _safe_root(self) -> None:
        ensure_no_symlinks(self.root, self.boundary_root.parent)

    def _resolve_target(self, relative_path: str) -> Path:
        rel = validate_relative_markdown_path(relative_path)
        self._safe_root()
        target = self.root.joinpath(*rel.parts)
        ensure_no_symlinks(target, self.boundary_root.parent)
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Document path escapes its collection directory")
        return target

    def _record_from_document(self, document) -> dict[str, Any]:
        target = self._resolve_target(document.location)
        raw = target.read_bytes()
        current = read_document(target, root=self.root, module=self.module, collection=self.collection)
        if current != document or raw != target.read_bytes():
            raise ValueError("Document changed during read; retry")
        metadata = document.metadata
        return {
            "id": document.key.id,
            "collection": self.collection,
            "module": self.module,
            "type": metadata.get("type", ""),
            "title": metadata.get("title", document.key.id),
            "description": metadata.get("description", ""),
            "path": document.location,
            "full_path": str(target),
            "content": document.text,
            "revision": hashlib.sha256(raw).hexdigest(),
            "content_revision": document.revision,
            "metadata": metadata,
            "key": asdict(document.key),
        }

    def scan(self, *, validate: Callable[[Any], None] | None = None) -> SourceSnapshot:
        """Capture a complete validated snapshot without creating missing directories."""
        self._safe_root()
        if not self.root.exists():
            return SourceSnapshot(self.module, self.collection, (), complete=True)
        return scan_markdown(self.root, module=self.module, collection=self.collection, validate=validate)

    def read(self, relative_path: str) -> dict[str, Any]:
        """Read a single Markdown/YAML document by relative path with full-file revision."""
        target = self._resolve_target(relative_path)
        if not target.exists():
            raise FileNotFoundError(f"Missing document: {relative_path}")
        document = read_document(target, root=self.root, module=self.module, collection=self.collection)
        return self._record_from_document(document)

    def save(
        self,
        relative_path: str,
        metadata: dict[str, Any],
        content: str,
        *,
        expected_revision: str | None,
        validate: Callable[[Any], None] | None = None,
        lock_ctx=None,
    ) -> dict[str, Any]:
        """Atomically save a Markdown/YAML document after revision, ID and schema checks."""
        raw = render_markdown_document(metadata, content)
        parsed = parse_document(raw, location=relative_path, module=self.module, collection=self.collection)
        if validate is not None:
            validate(parsed)

        guard = lock_ctx if lock_ctx is not None else self.layout.guard()
        with guard:
            target = self._resolve_target(relative_path)
            previous = target.read_bytes() if target.exists() else None
            actual = hashlib.sha256(previous).hexdigest() if previous is not None else None
            if expected_revision != actual:
                raise ValueError("Document changed; read its current revision")
            if self.root.exists():
                for existing in self.scan().documents:
                    if existing.key.id == metadata["id"] and self._resolve_target(existing.location) != target:
                        raise ValueError("Duplicate document identity: " + metadata["id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".document-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                observed = target.read_bytes() if target.exists() else None
                if observed != previous:
                    raise ValueError("Document changed; read its current revision")
                os.replace(temporary, target)
                sync_directory(target.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            saved = read_document(target, root=self.root, module=self.module, collection=self.collection)
            return self._record_from_document(saved)

    def delete(self, relative_path: str, *, expected_revision: str, lock_ctx=None) -> dict[str, Any]:
        """Delete a document only if its full-file revision matches expected_revision."""
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision is required to delete a document")
        guard = lock_ctx if lock_ctx is not None else self.layout.guard()
        with guard:
            target = self._resolve_target(relative_path)
            if not target.exists():
                raise FileNotFoundError(f"Missing document: {relative_path}")
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected_revision:
                raise ValueError("Document changed; read its current revision")
            target.unlink()
            sync_directory(target.parent)
            return {
                "deleted": True,
                "collection": self.collection,
                "path": relative_path,
                "full_path": str(target),
            }
