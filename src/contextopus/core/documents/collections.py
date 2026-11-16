"""Unified Core-owned file collections (instructions and arbitrary document collections)."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable

from ..rag import DocumentKey
from .file_store import DocumentFileStore
from .files import parse_document
from .projection import reconcile_index
from .reconcile import SourceSnapshot

_COLLECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

WELL_KNOWN_TITLES = {
    "instructions": "Shared instructions",
    "contexts": "Contexts",
    "notes": "Notes",
    "profiles": "Agent profiles",
    "teams": "Agent teams",
    "skills": "Skill descriptions",
    "skill_sources": "Skill sources",
    "session_summaries": "Session summaries",
    "images": "Image library",
}


def validate_collection_name(collection: str) -> str:
    """Validate a single-segment Core collection identifier."""
    if not isinstance(collection, str) or not _COLLECTION_NAME_RE.match(collection):
        raise ValueError(f"Invalid collection name: {collection!r}")
    return collection


def validate_instruction_document(document) -> None:
    """Validate instruction YAML front matter in the unified Core format."""
    metadata = document.metadata
    if metadata.get("type") != "instruction":
        raise ValueError("Instruction type must be instruction")
    for name in ("title", "description"):
        if name in metadata and not isinstance(metadata[name], str):
            raise ValueError(f"{name} must be text")
    if "config" in metadata and not isinstance(metadata["config"], dict):
        raise ValueError("config must be a mapping")
    for field in ("requires_rule", "requires_read", "references"):
        if field in metadata:
            value = metadata[field]
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError(f"{field} must be a list of strings")
    if "instructions" in metadata:
        required = metadata["instructions"]
        if not isinstance(required, dict):
            raise ValueError("instructions must map roles to wikilinks")
        for role, reference in required.items():
            if not isinstance(role, str) or not isinstance(reference, str) or not (
                reference.startswith("[[") and reference.endswith("]]")
                and reference[2:-2].split("|", 1)[0].strip()
            ):
                raise ValueError("instructions must map roles to wikilinks")
    json.dumps(metadata, allow_nan=False)


class DocumentCollections:
    """Own all ``module='core'`` Markdown/YAML collections without requiring plugins."""

    def __init__(self, layout, index=None):
        self.layout = layout
        self.index = index
        self.instructions_root = layout.root / "core" / "instructions"
        self.documents_root = layout.root / "core" / "documents"
        if index is not None:
            index.register_source("core", self)

    def collection_root(self, collection: str) -> Path:
        validate_collection_name(collection)
        if collection == "instructions":
            return self.instructions_root
        return self.documents_root / collection

    def store(self, collection: str) -> DocumentFileStore:
        root = self.collection_root(collection)
        return DocumentFileStore(self.layout, module="core", collection=collection, root=root)

    def _validator(self, collection: str) -> Callable[[Any], None] | None:
        if collection == "instructions":
            return validate_instruction_document
        return None

    def _existing_collection_names(self) -> list[str]:
        names: list[str] = []
        if self.instructions_root.exists() and self.instructions_root.is_dir():
            names.append("instructions")
        if self.documents_root.exists() and self.documents_root.is_dir():
            if self.documents_root.is_symlink():
                raise ValueError("Core documents directory cannot be a symlink")
            for entry in sorted(self.documents_root.iterdir(), key=lambda p: p.name):
                if entry.name.startswith("."):
                    continue
                if entry.is_symlink():
                    raise ValueError("Collection directory cannot be a symlink: " + entry.name)
                if entry.is_dir():
                    validate_collection_name(entry.name)
                    names.append(entry.name)
        return names

    def collections(self) -> list[dict[str, Any]]:
        """List discovered Core collections (plus instructions) without creating dirs."""
        existing = self._existing_collection_names()
        if "instructions" not in existing:
            existing = ["instructions", *existing]
        result = []
        for name in existing:
            root = self.collection_root(name)
            result.append({
                "id": name,
                "title": WELL_KNOWN_TITLES.get(name, name.replace("_", " ").replace("-", " ").title()),
                "path": "core/instructions" if name == "instructions" else f"core/documents/{name}",
                "exists": root.exists(),
            })
        return result

    def snapshot(self, collection: str) -> SourceSnapshot:
        return self.store(collection).scan(validate=self._validator(collection))

    def snapshots(self) -> tuple[SourceSnapshot, ...]:
        """Return validated snapshots for all existing Core collections."""
        names = self._existing_collection_names()
        if "instructions" not in names:
            names = ["instructions", *names]
        return tuple(self.snapshot(name) for name in names)

    def _find_document(self, collection: str, reference: str):
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Expected a document reference")
        name = reference.strip()
        if name.startswith("[[") and name.endswith("]]"):
            name = name[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
        prefix = "core/instructions/" if collection == "instructions" else f"core/documents/{collection}/"
        if name.startswith(prefix):
            name = name[len(prefix):]
        docs = self.snapshot(collection).documents
        exact_matches = [
            doc for doc in docs
            if name in {
                doc.key.id,
                doc.location,
                str(PurePosixPath(doc.location).with_suffix("")),
                doc.metadata.get("slug"),
                *(
                    [PurePosixPath(doc.location).parent.as_posix()]
                    if PurePosixPath(doc.location).name in {"Context.md", "index.md", "team.md"}
                    and PurePosixPath(doc.location).parent.as_posix() != "."
                    else []
                ),
            }
        ]
        matches = exact_matches or [
            doc for doc in docs
            if name == PurePosixPath(doc.location).stem
        ]
        if not matches:
            raise FileNotFoundError(f"Missing document in {collection}: {name}")
        if len(matches) != 1:
            raise ValueError(f"Ambiguous document in {collection}: {name}")
        return matches[0]

    def read(self, collection: str, reference: str) -> dict[str, Any]:
        """Read full document record by ID, relative path, or unique stem."""
        doc = self._find_document(collection, reference)
        return self.store(collection)._record_from_document(doc)

    def list(self, collection: str | None = None) -> list[dict[str, Any]]:
        """List full document records for one collection or all existing Core collections."""
        targets = [validate_collection_name(collection)] if collection else self._existing_collection_names()
        items: list[dict[str, Any]] = []
        for coll in targets:
            store = self.store(coll)
            for doc in self.snapshot(coll).documents:
                items.append(store._record_from_document(doc))
        return items

    def _reconcile_collection(self, collection: str) -> dict[str, Any]:
        if self.index is None:
            return {"index_pending": False, "indexed": 0, "refreshed": 0, "deleted": 0, "unchanged": 0}
        try:
            snap = self.snapshot(collection)
            res = reconcile_index(self.index, snap)
            return {"index_pending": False, **res}
        except Exception as exc:
            return {"index_pending": True, "index_error": str(exc)}

    def _index_single_document(self, collection: str, relative_path: str) -> dict[str, Any]:
        if self.index is None:
            return {"index_pending": False, "indexed": 0, "refreshed": 0, "deleted": 0, "unchanged": 0}
        try:
            store = self.store(collection)
            target = store._resolve_target(relative_path)
            doc = parse_document(
                target.read_bytes(),
                location=relative_path,
                module="core",
                collection=collection,
            )
            self.index.index(doc)
            return {"index_pending": False, "indexed": 1, "refreshed": 0, "deleted": 0, "unchanged": 0}
        except Exception as exc:
            return {"index_pending": True, "index_error": str(exc)}

    def _history(self, message=None, collection=None, relative_path=None):
        try:
            from ..settings import Settings
            from ..history import GitRepository
            context = relative_path.split('/')[0] if collection == 'notes' and relative_path and '/' in relative_path else None
            if context:
                try:
                    context = self.read('contexts', context)['id']
                except FileNotFoundError:
                    pass
            if Settings(self.layout).effective_for_context(context).get('git_history_enabled', True):
                repo = GitRepository(self.layout.root / 'core')
                return repo.commit_all(message) if message else repo.ensure()
        except Exception:
            pass

    def save(
        self,
        collection: str,
        relative_path: str,
        metadata: dict[str, Any],
        content: str,
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Save document on disk and update only that document in the shared index."""
        validate_collection_name(collection)
        store = self.store(collection)
        self._history(collection=collection, relative_path=relative_path)
        record = store.save(
            relative_path,
            metadata,
            content,
            expected_revision=expected_revision,
            validate=self._validator(collection),
        )
        idx_status = self._index_single_document(collection, relative_path)
        self._history("Save " + collection + "/" + relative_path, collection, relative_path)
        return {**record, "saved": True, **idx_status}

    def delete(self, collection: str, relative_path: str, *, expected_revision: str) -> dict[str, Any]:
        """Delete document on disk and remove only that document from the shared index."""
        validate_collection_name(collection)
        store = self.store(collection)
        self._history(collection=collection, relative_path=relative_path)
        doc_id = store.read(relative_path)["id"]
        result = store.delete(relative_path, expected_revision=expected_revision)
        if self.index is None:
            idx_status = {"index_pending": False}
        else:
            try:
                self.index.delete_document(DocumentKey("core", collection, doc_id))
                idx_status = {"index_pending": False, "index_deleted": 1}
            except Exception as exc:
                idx_status = {"index_pending": True, "index_error": str(exc)}
        self._history("Delete " + collection + "/" + relative_path, collection, relative_path)
        return {**idx_status, **result}

    def reconcile(self, collection: str | None = None) -> dict[str, Any]:
        """Explicitly reconcile one or all Core collections against the shared index."""
        self._history()
        if collection in (None, 'skills', 'skill_sources'):
            from .reconcile import sync_skill_sources
            sync_skill_sources(self)
        targets = [validate_collection_name(collection)] if collection else self._existing_collection_names()
        if collection == 'skill_sources' and 'skills' not in targets:
            targets.append('skills')
        if "instructions" not in targets and not collection:
            targets = ["instructions", *targets]
        summary = {"collections": [], "indexed": 0, "refreshed": 0, "deleted": 0, "unchanged": 0}
        for coll in targets:
            snap = self.snapshot(coll)
            if self.index is not None:
                res = reconcile_index(self.index, snap)
                summary["indexed"] += res["indexed"]
                summary["refreshed"] += res["refreshed"]
                summary["deleted"] += res["deleted"]
                summary["unchanged"] += res["unchanged"]
                summary["collections"].append({"collection": coll, **res})
            else:
                summary["collections"].append({"collection": coll, "documents": len(snap.documents)})
        self._history("Reconcile documents")
        return summary
