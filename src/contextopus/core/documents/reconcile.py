"""Plan derived-store changes without interpreting a failed scan as deletion."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

import yaml

from ..rag import DocumentKey, IndexDocument


@dataclass(frozen=True)
class SourceSnapshot:
    module: str
    collection: str
    documents: tuple[IndexDocument, ...]
    complete: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reconciliation:
    upsert: tuple[IndexDocument, ...]
    refresh: tuple[IndexDocument, ...]
    delete: tuple[DocumentKey, ...]
    unchanged: tuple[DocumentKey, ...]


def sync_skill_sources(collections) -> int:
    """Refresh catalog cards for skills found in configured local sources."""
    sources = collections.list("skill_sources")
    existing = collections.list("skills")
    changed = 0
    for source in sources:
        meta = source["metadata"]
        root = meta.get("root") or meta.get("path") or meta.get("location")
        if not isinstance(root, str) or not Path(root).is_dir():
            continue
        for skill_file in sorted(Path(root).glob("*/SKILL.md")):
            try:
                lines = skill_file.read_text(encoding="utf-8").splitlines()
                if not lines or lines[0].strip() != "---":
                    continue
                end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
                frontmatter = yaml.safe_load("\n".join(lines[1:end]))
                if not isinstance(frontmatter, dict):
                    continue
                title, description = frontmatter.get("name"), frontmatter.get("description", "")
                if not isinstance(title, str) or not title.strip() or not isinstance(description, str):
                    continue
                location = str(skill_file)
                card = next((item for item in existing if item["metadata"].get("location") == location), None)
                if card is None:
                    card = next((item for item in existing if item["metadata"].get("source_id") == source["id"]
                                 and item["title"] == title), None)
                fields = {"source_id": source["id"], "location": location, "title": title,
                          "description": description, "status": "active"}
                if card:
                    metadata = {**card["metadata"], **fields}
                    if metadata == card["metadata"]:
                        continue
                    path, revision, content = card["path"], card["revision"], card["content"]
                else:
                    ident = uuid5(NAMESPACE_URL, location).hex
                    metadata = {"id": ident, "type": "skill", **fields}
                    path, revision, content = f"{ident}.md", None, ""
                saved = collections.store("skills").save(path, metadata, content, expected_revision=revision)
                if card:
                    existing.remove(card)
                existing.append(saved)
                changed += 1
            except (OSError, UnicodeError, StopIteration, yaml.YAMLError):
                continue
    return changed


def _documents(items, scope):
    result = {}
    for document in items:
        key = document.key
        if (key.module, key.collection) != scope:
            raise ValueError("Document is outside the declared source scope")
        if not key.id or not document.revision:
            raise ValueError("Documents require stable identity and revision")
        if key in result:
            raise ValueError("Duplicate document identity: " + key.id)
        result[key] = document
    return result


def plan(snapshot: SourceSnapshot, previous: Iterable[IndexDocument]) -> Reconciliation:
    """Compare one collection; callers serialize capture/apply with source writes.

    A partial snapshot can add/update but cannot infer deletions. Failed scans
    abort before any effects. SQL and RAG adapters apply the same plan to their
    own projections; this function makes no cross-database atomicity claim.
    Revisions identify indexed text; metadata/path-only changes need no embedding.
    """
    if not snapshot.module or not snapshot.collection:
        raise ValueError("An explicit module and collection are required")
    if type(snapshot.complete) is not bool:
        raise ValueError("Snapshot completeness must be boolean")
    if snapshot.errors:
        raise ValueError("Source scan failed: " + "; ".join(snapshot.errors))
    scope = snapshot.module, snapshot.collection
    current = _documents(snapshot.documents, scope)
    old = _documents(previous, scope)
    upsert, refresh, unchanged = [], [], []
    for key, document in current.items():
        prior = old.get(key)
        if prior is None or prior.revision != document.revision:
            upsert.append(document)
        elif prior.location != document.location or prior.metadata != document.metadata:
            refresh.append(document)
        else:
            unchanged.append(key)
    deleted = tuple(key for key in old if key not in current) if snapshot.complete else ()
    return Reconciliation(tuple(upsert), tuple(refresh), deleted, tuple(unchanged))
