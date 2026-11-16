"""Cross-collection wikilink and Markdown link codec over Core DocumentCollections."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any, Iterable
from urllib.parse import unquote

from ..rag import DocumentKey, IndexDocument

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def _vault_root(root: Path) -> Path:
    resolved = Path(root).resolve()
    candidates = (
        [resolved]
        if (resolved / ".obsidian").is_dir()
        else [p for p in resolved.parents if (p / ".obsidian").is_dir()]
    )
    if len(candidates) > 1:
        raise ValueError("Several enclosing Obsidian vaults exist; select one unambiguous root")
    return candidates[0] if candidates else resolved


def document_root_relative_path(document: IndexDocument) -> str:
    """Return root-relative POSIX path for a Core document."""
    if document.key.collection == "instructions":
        return f"core/instructions/{document.location}"
    return f"core/documents/{document.key.collection}/{document.location}"


def strip_wikilink_wrappers(value: str) -> tuple[str, str | None, str | None]:
    """Extract (target, anchor, label) from '[[target#anchor|label]]' or bare reference."""
    raw = value.strip()
    if raw.startswith("[["):
        if not raw.endswith("]]"):
            raise ValueError(f"Invalid wikilink: {value}")
        inner = raw[2:-2]
        path_part, _, label = inner.partition("|")
        base, sep, anchor = path_part.partition("#")
        return base.strip(), (anchor.strip() if sep else None), (label.strip() if "|" in inner else None)
    base, sep, anchor = raw.partition("#")
    return base.strip(), (anchor.strip() if sep else None), None


class LinkCodec:
    """Resolve and render links across all Core file collections without a separate graph DB."""

    def __init__(self, root: Path, documents: Iterable[IndexDocument]):
        self.root = Path(root).resolve()
        self.vault_root = _vault_root(self.root)
        self.documents_by_key: dict[DocumentKey, IndexDocument] = {}
        self.paths: dict[DocumentKey, str] = {}
        self.by_path: dict[str, list[DocumentKey]] = {}
        self.by_id: dict[str, list[DocumentKey]] = {}
        self.by_collection_rel: dict[tuple[str, str], list[DocumentKey]] = {}

        for doc in documents:
            if doc.key in self.documents_by_key:
                raise ValueError(f"Duplicate document key: {doc.key}")
            self.documents_by_key[doc.key] = doc
            abs_path = self.root / document_root_relative_path(doc)
            rel_path = abs_path.relative_to(self.vault_root).as_posix()
            self.paths[doc.key] = rel_path
            self.by_path.setdefault(rel_path, []).append(doc.key)
            self.by_id.setdefault(doc.key.id, []).append(doc.key)
            self.by_collection_rel.setdefault((doc.key.collection, doc.location), []).append(doc.key)
            if doc.location.endswith(".md"):
                self.by_collection_rel.setdefault((doc.key.collection, doc.location[:-3]), []).append(doc.key)
            loc_path = PurePosixPath(doc.location)
            if loc_path.name in {"Context.md", "index.md", "team.md"} and loc_path.parent.as_posix() != ".":
                self.by_collection_rel.setdefault((doc.key.collection, loc_path.parent.as_posix()), []).append(doc.key)
            slug = doc.metadata.get("slug")
            if isinstance(slug, str) and slug.strip():
                self.by_collection_rel.setdefault((doc.key.collection, slug.strip()), []).append(doc.key)

    def resolve_reference(
        self,
        reference: str,
        *,
        source_key: DocumentKey | None = None,
        default_collection: str | None = None,
    ) -> DocumentKey:
        """Resolve a wikilink or ID/path reference to a unique DocumentKey."""
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Reference must be a non-empty string")
        target_str, _anchor, _label = strip_wikilink_wrappers(reference)
        if not target_str or any(c in target_str for c in "[]^\\") or target_str.startswith("/"):
            raise ValueError(f"Invalid document link target: {reference}")

        # 1. Exact collection-scoped match if default_collection provided and bare ID/relative path
        if default_collection and "/" not in target_str:
            scoped = self.by_collection_rel.get((default_collection, target_str), [])
            if len(scoped) == 1:
                return scoped[0]

        # 2. Exact unique document ID across all Core collections
        if not reference.strip().startswith("[[") and "/" not in target_str:
            id_matches = self.by_id.get(target_str, [])
            if len(id_matches) == 1:
                return id_matches[0]
            if len(id_matches) > 1:
                raise ValueError(f"Ambiguous document ID {reference!r}; specify collection path")

        md_target = target_str if target_str.endswith(".md") else target_str + ".md"
        parts = PurePosixPath(md_target).parts

        # 3. Relative navigation with '..' from source document
        if ".." in parts:
            if source_key is None or source_key not in self.paths:
                raise ValueError(f"Relative wikilink has no source document: {reference}")
            rel = posixpath.normpath(str(PurePosixPath(self.paths[source_key]).parent / md_target))
            if rel == ".." or rel.startswith("../"):
                raise ValueError(f"Link path escapes vault root: {reference}")
            matches = self.by_path.get(rel, [])
        else:
            # 4. Exact vault-relative path
            matches = self.by_path.get(md_target, [])
            # 5. Root-relative Core path (in case vault_root encloses root)
            if not matches:
                try:
                    prefixed = (self.root / md_target).relative_to(self.vault_root).as_posix()
                    matches = self.by_path.get(prefixed, [])
                except ValueError:
                    pass
            # 6. Source-relative path
            if not matches and source_key is not None and source_key in self.paths:
                parent = PurePosixPath(self.paths[source_key]).parent
                matches = self.by_path.get((parent / md_target).as_posix(), [])
            # 7. Unique suffix match
            if not matches:
                suffix = "/" + md_target
                matches = [
                    key
                    for path, keys in self.by_path.items()
                    if path == md_target or path.endswith(suffix)
                    for key in keys
                ]
            # 8. Unique ID fallback even inside [[...]]
            if not matches and "/" not in target_str:
                matches = self.by_id.get(target_str, [])

        if len(matches) != 1:
            problem = "Ambiguous" if matches else "Unresolved"
            raise ValueError(f"{problem} link {reference!r}")
        return matches[0]

    def encode(self, key: DocumentKey, *, alias: str | None = None) -> str:
        """Render a native wikilink for a known DocumentKey."""
        if key not in self.paths:
            raise ValueError(f"Cannot render missing link target: {key}")
        path = self.paths[key]
        if any(c in path for c in "[]|#^\n\r"):
            raise ValueError(f"Target path cannot be represented by a native wikilink: {path}")
        label = alias if alias is not None else PurePosixPath(path).stem
        return f"[[{path}|{label}]]"

    @staticmethod
    def extract_raw_references(document: IndexDocument) -> list[tuple[str, str]]:
        """Extract (field_or_origin, raw_reference) pairs from YAML metadata and Markdown body."""
        refs: list[tuple[str, str]] = []
        meta = document.metadata or {}
        for field in ("requires_rule", "requires_read", "references", "links"):
            val = meta.get(field)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        refs.append((field, item.strip()))
        legacy_instr = meta.get("instructions")
        if isinstance(legacy_instr, dict):
            for _role, ref in legacy_instr.items():
                if isinstance(ref, str) and ref.strip():
                    refs.append(("instructions", ref.strip()))
        roles = meta.get("roles")
        if isinstance(roles, dict):
            for _role, cfg in roles.items():
                if isinstance(cfg, dict) and isinstance(cfg.get("profile"), str) and cfg["profile"].strip():
                    refs.append(("roles", cfg["profile"].strip()))
        for match in _WIKILINK_RE.finditer(document.text or ""):
            refs.append(("body", f"[[{match.group(1)}]]"))
        for match in _MD_LINK_RE.finditer(document.text or ""):
            raw = unquote(match.group(1)).strip()
            if not raw or "://" in raw or raw.startswith(("#", "mailto:")):
                continue
            refs.append(("body", raw))
        return refs
