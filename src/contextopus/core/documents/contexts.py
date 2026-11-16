"""Read a Contextopus context together with its resolved rules and required reads."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import PurePosixPath
from typing import Any

from ..rag import DocumentKey
from .collections import DocumentCollections
from .link_codec import LinkCodec, document_root_relative_path


class ContextReader:
    """Resolve a context document, its active rules, required read data, and settings."""

    def __init__(self, collections: DocumentCollections, instructions, settings=None):
        self.collections = collections
        self.instructions = instructions
        self.settings = settings

    def read(self, id: str | None = None, *, expected_revision: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        """Read context, verify optional revision, resolve rules and required_reads (data only)."""
        if not (isinstance(id, str) and id.strip()) and not (isinstance(cwd, str) and cwd.strip()):
            raise ValueError("Context id or slug is required")
        record = None
        resolved_workdir = None
        if id and not id.startswith("/"):
            try:
                record = self.collections.read("contexts", id)
            except FileNotFoundError:
                pass
        if record is None:
            candidate = cwd or id
            if isinstance(candidate, str) and PurePosixPath(candidate).is_absolute():
                path = PurePosixPath(candidate)
                matches = []
                for doc in self.collections.snapshot("contexts").documents:
                    meta = doc.metadata
                    workdirs = meta.get("workdirs") if isinstance(meta.get("workdirs"), list) else []
                    workdirs = [*workdirs, meta.get("workdir")]
                    for workdir in workdirs:
                        if isinstance(workdir, str) and PurePosixPath(workdir).is_absolute():
                            root = PurePosixPath(workdir)
                            if ".." not in root.parts and ".." not in path.parts and path.is_relative_to(root):
                                matches.append((len(root.parts), doc.key.id, workdir))
                if matches:
                    _, context_id, resolved_workdir = max(matches, key=lambda item: item[0])
                    record = self.collections.read("contexts", context_id)
        if record is None:
            raise FileNotFoundError(f"Missing context: {cwd or id}")
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ValueError("Context changed; read its current revision")

        meta = record.get("metadata") or {}
        rule_refs: list[str] = []
        if isinstance(meta.get("requires_rule"), list):
            rule_refs.extend(r for r in meta["requires_rule"] if isinstance(r, str) and r.strip())
        if isinstance(meta.get("instructions"), dict):
            rule_refs.extend(r for r in meta["instructions"].values() if isinstance(r, str) and r.strip())

        resolved_rules = self.instructions.resolve(rule_refs) if rule_refs else {
            "instructions": [],
            "revision": None,
        }

        # Build cross-collection LinkCodec once for required_reads and references.
        snapshots = self.collections.snapshots()
        all_docs = [doc for snap in snapshots for doc in snap.documents]
        codec = LinkCodec(self.collections.layout.root, all_docs)
        ctx_key = DocumentKey("core", "contexts", record["id"])

        required_reads: list[dict[str, Any]] = []
        seen_reads: set[DocumentKey] = set()
        raw_req_read = meta.get("requires_read") or []
        if not isinstance(raw_req_read, list):
            raise ValueError("requires_read must be a list of references")
        for ref in raw_req_read:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("requires_read entries must be non-empty strings")
            target_key = codec.resolve_reference(ref, source_key=ctx_key)
            if target_key in seen_reads:
                continue
            seen_reads.add(target_key)
            data_rec = self.collections.read(target_key.collection, target_key.id)
            required_reads.append({
                "id": data_rec["id"],
                "collection": data_rec["collection"],
                "type": data_rec["type"],
                "title": data_rec["title"],
                "path": f"core/documents/{data_rec['collection']}/{data_rec['path']}"
                if data_rec["collection"] != "instructions"
                else f"core/instructions/{data_rec['path']}",
                "revision": data_rec["revision"],
                "content": data_rec["content"],
                "metadata": data_rec["metadata"],
                "activation": "data_only",
            })

        route_map: list[dict[str, Any]] = []
        routed_keys: set[DocumentKey] = set()
        raw_routes = meta.get("rule_routes") or []
        if isinstance(raw_routes, list):
            for entry in raw_routes:
                if not isinstance(entry, dict):
                    continue
                target_ref = entry.get("target")
                when_text = entry.get("when")
                if not isinstance(target_ref, str) or not target_ref.strip():
                    continue
                try:
                    target_key = codec.resolve_reference(target_ref, source_key=ctx_key)
                    target_doc = codec.documents_by_key[target_key]
                    routed_keys.add(target_key)
                    route_map.append({
                        "when": str(when_text).strip() if isinstance(when_text, str) and when_text.strip()
                        else target_doc.metadata.get("description") or target_doc.metadata.get("title", target_key.id),
                        "target": target_ref,
                        "key": asdict(target_key),
                        "path": document_root_relative_path(target_doc),
                        "title": target_doc.metadata.get("title", target_key.id),
                        "description": target_doc.metadata.get("description", ""),
                        "mandatory_when_matching": True,
                    })
                except ValueError as exc:
                    route_map.append({
                        "when": str(when_text or ""),
                        "target": target_ref,
                        "unresolved": True,
                        "error": str(exc),
                    })

        references: list[dict[str, Any]] = []
        for field in ("references", "links"):
            raw_refs = meta.get(field) or []
            if not isinstance(raw_refs, list):
                continue
            for ref in raw_refs:
                if not isinstance(ref, str) or not ref.strip():
                    continue
                try:
                    target_key = codec.resolve_reference(ref, source_key=ctx_key)
                    target_doc = codec.documents_by_key[target_key]
                    ref_item = {
                        "reference": ref,
                        "key": asdict(target_key),
                        "path": document_root_relative_path(target_doc),
                        "title": target_doc.metadata.get("title", target_key.id),
                    }
                    references.append(ref_item)
                    if target_key not in routed_keys:
                        routed_keys.add(target_key)
                        route_map.append({
                            "when": target_doc.metadata.get("description") or target_doc.metadata.get("title", target_key.id),
                            "target": ref,
                            "key": asdict(target_key),
                            "path": document_root_relative_path(target_doc),
                            "title": target_doc.metadata.get("title", target_key.id),
                            "description": target_doc.metadata.get("description", ""),
                            "mandatory_when_matching": True,
                        })
                except ValueError as exc:
                    references.append({"reference": ref, "unresolved": True, "error": str(exc)})

        context_settings = {}
        if self.settings is not None and hasattr(self.settings, "effective_for_context"):
            context_settings = self.settings.effective_for_context(record["id"])
        else:
            cfg_path = self.collections.layout.configuration / "contexts" / "core" / f"{record['id']}.json"
            if cfg_path.exists():
                context_settings = json.loads(cfg_path.read_text(encoding="utf-8"))

        result = {
            "settings": context_settings,
            "routing_directive": (
                "Before acting on a sub-problem, open every route_map target whose 'when' or 'title' "
                "matches the task via core.library(action='record', ...) and apply it as a mandatory standard."
            ),
            "context": {
                "id": record["id"],
                "type": record["type"],
                "title": record["title"],
                "description": record["description"],
                "path": f"core/documents/contexts/{record['path']}",
                "revision": record["revision"],
                "content": record["content"],
                "metadata": meta,
            },
            "rules": resolved_rules["instructions"],
            "rules_revision": resolved_rules["revision"],
            "route_map": route_map,
            "required_reads": required_reads,
            "references": references,
        }
        if resolved_workdir is not None:
            result["resolved_workdir"] = resolved_workdir
        return result
