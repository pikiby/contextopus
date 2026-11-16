"""Exact wikilink resolution and relation diagnostics across Core documents."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any, Iterable

from ..rag import DocumentKey, IndexDocument
from .link_codec import LinkCodec, strip_wikilink_wrappers


def direct_relations(anchors, edges, offset=0, limit=50):
    """Page one-hop relations supplied by an owner, without reading documents.

    An edge is a pair of DocumentKeys (source, target). Reciprocal edges have
    direction ``both``; repeated anchors and edges do not duplicate rows.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    selected = set(anchors)
    if any(not isinstance(key, DocumentKey) for key in selected):
        raise ValueError("anchors must contain DocumentKeys")
    neighbors = {}
    for source, target in edges:
        if not isinstance(source, DocumentKey) or not isinstance(target, DocumentKey):
            raise ValueError("edges must contain pairs of DocumentKeys")
        if source == target:
            continue
        if source in selected:
            neighbors.setdefault((source, target), set()).add("outgoing")
        if target in selected:
            neighbors.setdefault((target, source), set()).add("incoming")

    def order(pair):
        return tuple((key.module, key.collection, key.id) for key in pair)

    pairs = sorted(neighbors, key=order)
    rows = [
        {
            "anchor": anchor,
            "key": key,
            "direction": (
                "both"
                if len(neighbors[(anchor, key)]) == 2
                else next(iter(neighbors[(anchor, key)]))
            ),
        }
        for anchor, key in pairs[offset : offset + limit]
    ]
    return {
        "items": rows,
        "total": len(pairs),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(pairs) else None,
    }


def _required_rule_refs(document: IndexDocument) -> list[str]:
    """Return explicit requires_rule references (plus legacy instructions mapping if present)."""
    refs: list[str] = []
    meta = document.metadata or {}
    req = meta.get("requires_rule")
    if req is not None:
        if not isinstance(req, list):
            raise ValueError("requires_rule must be a list of references")
        for item in req:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("requires_rule entries must be non-empty strings")
            refs.append(item.strip())
    legacy = meta.get("instructions")
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise ValueError("instructions must map roles to wikilinks")
        for ref in legacy.values():
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("instructions must map roles to wikilinks")
            refs.append(ref.strip())
    return refs


def resolve_required(documents: Iterable[IndexDocument], references: Iterable[str]):
    """Return full instruction documents, dependencies first, once each; never use similarity.

    Expands only ``requires_rule`` (and legacy ``instructions`` mapping). Ordinary
    ``references``, ``links``, and ``requires_read`` are never activated as rules.
    """
    aliases: dict[str, list[IndexDocument]] = {}
    identities: set[DocumentKey] = set()
    for document in documents:
        if document.key in identities:
            raise ValueError("Duplicate instruction identity")
        identities.add(document.key)
        rel = document.location
        prefixed = (
            f"core/instructions/{rel}"
            if document.key.collection == "instructions"
            else f"core/documents/{document.key.collection}/{rel}"
        )
        for name in {
            document.key.id,
            rel,
            prefixed,
            str(PurePosixPath(rel).with_suffix("")),
            str(PurePosixPath(prefixed).with_suffix("")),
            PurePosixPath(rel).stem,
        }:
            aliases.setdefault(name, []).append(document)

    active: set[DocumentKey] = set()
    done: set[DocumentKey] = set()
    result: list[IndexDocument] = []

    def visit(reference: str) -> None:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Expected an instruction reference")
        name, _anchor, _label = strip_wikilink_wrappers(reference)
        targets = aliases.get(name, [])
        if not targets and not name.endswith(".md"):
            targets = aliases.get(name + ".md", [])
        if len(targets) != 1:
            raise ValueError("Missing or ambiguous instruction: " + name)
        document = targets[0]
        if document.metadata.get("type") != "instruction":
            raise ValueError("Required target is not an instruction: " + name)
        key = document.key
        if key in active:
            raise ValueError("Cyclic required instructions: " + name)
        if key in done:
            return
        active.add(key)
        for dependency in _required_rule_refs(document):
            visit(dependency)
        active.remove(key)
        done.add(key)
        result.append(document)

    for reference in references:
        visit(reference)
    return tuple(result)


def collect_relations(collections, target_key: DocumentKey) -> dict[str, Any]:
    """Collect incoming, outgoing and unresolved relations for a Core document across all collections."""
    snapshots = collections.snapshots()
    complete = all(snap.complete and not snap.errors for snap in snapshots)
    all_docs = [doc for snap in snapshots for doc in snap.documents]
    codec = LinkCodec(collections.layout.root, all_docs)

    incoming: list[dict[str, Any]] = []
    outgoing: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for doc in all_docs:
        for origin, raw_ref in codec.extract_raw_references(doc):
            try:
                resolved = codec.resolve_reference(
                    raw_ref,
                    source_key=doc.key,
                    default_collection="instructions" if origin in {"requires_rule", "instructions"} else None,
                )
            except ValueError as exc:
                if doc.key == target_key:
                    unresolved.append({"origin": origin, "reference": raw_ref, "error": str(exc)})
                continue
            if resolved == doc.key:
                continue
            if doc.key == target_key:
                outgoing.append({"key": asdict(resolved), "origin": origin, "reference": raw_ref})
            if resolved == target_key:
                incoming.append({"key": asdict(doc.key), "origin": origin, "reference": raw_ref})

    return {
        "target": asdict(target_key),
        "incoming": incoming,
        "outgoing": outgoing,
        "unresolved": unresolved,
        "complete": complete,
    }
