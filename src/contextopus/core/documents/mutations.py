"""Revision-bound document preview, save and delete with owner-scoped approval policies."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from ..rag import DocumentKey
from .collections import DocumentCollections, validate_collection_name, validate_instruction_document
from .file_store import render_markdown_document, validate_relative_markdown_path
from .files import parse_document
from .links import collect_relations


class DocumentMutations:
    """Unified preview/save/delete pipeline for Core collections without Workspace cycles."""

    def __init__(self, collections: DocumentCollections, settings=None):
        self.collections = collections
        self.settings = settings

    def _find_similar(self, collection: str, path: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Find nearby cards within the new card's collection and context."""
        if collection in {"instructions", "contexts"} or self.collections.index is None:
            return []
        index = self.collections.index
        parts = PurePosixPath(path).parts
        prefix = f"{parts[0]}/" if len(parts) > 1 else ""
        scope = {"module": "core", "collection": collection}
        if prefix:
            scope["prefix"] = prefix
        query = " ".join(str(metadata.get(field) or "") for field in ("title", "description")).strip()
        if not query:
            return []
        try:
            response = index.search_detailed(
                query, scopes=[scope], include_archived=False,
                selection={"algorithm": "nearest_n", "parameters": {"N": 3}},
            )
            items = []
            for hit in response.get("results", []):
                hit_path = hit.get("path") or hit.get("location", "")
                if prefix and not hit_path.startswith(prefix):
                    continue
                key = hit.get("key", [])
                hit_metadata = hit.get("metadata") or {}
                items.append({
                    "id": key[2] if len(key) >= 3 else hit.get("id"),
                    "path": hit_path,
                    "title": hit_metadata.get("title", hit.get("title", "")),
                    "description": hit_metadata.get("description", hit.get("description", "")),
                    "distance": hit.get("distance"),
                    "revision": hit.get("revision"),
                })
            return items
        except Exception:
            return []

    def _owner_context_id(self, collection: str, relative_path: str, metadata: dict[str, Any] | None) -> str | None:
        """Resolve the owning context ID for policy lookup, never using caller-selected context."""
        if collection == "instructions":
            return None
        if collection == "contexts":
            doc_id = (metadata or {}).get("id")
            if isinstance(doc_id, str) and doc_id.strip():
                return doc_id.strip()
            return None
        if collection == "notes":
            parts = PurePosixPath(relative_path).parts
            if len(parts) >= 2:
                slug = parts[0]
                try:
                    ctx = self.collections.read("contexts", slug)
                    return ctx["id"]
                except (FileNotFoundError, ValueError):
                    return None
        return None

    def _approval_policy(self, collection: str, relative_path: str, metadata: dict[str, Any] | None) -> tuple[bool, str]:
        owner_ctx = self._owner_context_id(collection, relative_path, metadata)
        field = (
            "instructions_approval_required"
            if collection == "instructions" or (metadata or {}).get("type") == "instruction"
            else "documents_approval_required"
        )
        if self.settings is None:
            return True, f"default:{field}"
        snap = self.settings.snapshot(module="core" if owner_ctx else None, context=owner_ctx)
        required = bool(snap["values"].get(field, True))
        origin = snap["sources"].get(field, "default")
        source = f"context:{owner_ctx}:{field}" if owner_ctx and origin == "context.configuration" else origin
        return required, source

    def _preview_dir(self):
        return self.collections.layout.root / "runtime" / "document-previews"

    def _record_preview(self, change_revision: str, payload: dict[str, Any]) -> None:
        directory = self._preview_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{change_revision}.json"
        tmp = directory / f"{change_revision}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _consume_preview(self, change_revision: str) -> None:
        try:
            (self._preview_dir() / f"{change_revision}.json").unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _normalize_approval(approval: Any) -> dict[str, Any] | None:
        if hasattr(approval, "model_dump"):
            return approval.model_dump()
        return approval

    @staticmethod
    def _approval_template(change_revision: str, approval_required: bool) -> dict[str, str] | None:
        if not approval_required:
            return None
        return {
            "change_revision": change_revision,
            "confirmation": "<explicit user approval text>",
        }

    @staticmethod
    def _action_contract(approval_required: bool) -> str:
        if approval_required:
            return (
                "STOP: Present target path and exact unified diff in chat. "
                "Confirm this is a verified principle (not raw debugging parameters). "
                "Wait for explicit user approval of this specific diff before calling save/delete."
            )
        return "Verify this records an established fact/principle rather than temporary parameters before calling save/delete."

    @staticmethod
    def _change_revision(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def preview(
        self,
        collection: str,
        path: str,
        metadata: dict[str, Any] | None = None,
        content: str = "",
        *,
        expected_revision: str | None = None,
        action: str = "save",
    ) -> dict[str, Any]:
        """Validate a proposed save/delete, persist server preview receipt, and return change_revision and diff."""
        validate_collection_name(collection)
        validate_relative_markdown_path(path)
        if action not in ("save", "delete"):
            raise ValueError("Unsupported preview action; expected 'save' or 'delete'")

        store = self.collections.store(collection)
        target = store._resolve_target(path)

        if action == "delete":
            if not isinstance(expected_revision, str) or not expected_revision.strip():
                raise ValueError("expected_revision is required for delete preview")
            record = store.read(path)
            if record["revision"] != expected_revision:
                raise ValueError("Document changed; read its current revision")
            key = DocumentKey("core", collection, record["id"])
            relations = collect_relations(self.collections, key)
            if not relations["complete"]:
                raise ValueError("Cannot delete document while collection scan is incomplete")
            if relations["incoming"]:
                raise ValueError("Cannot delete document with incoming links")
            approval_required, policy_source = self._approval_policy(collection, path, record["metadata"])
            change_revision = self._change_revision({
                "action": "delete",
                "collection": collection,
                "path": path,
                "expected_revision": expected_revision,
                "approval_required": approval_required,
            })
            existing_text = target.read_text(encoding="utf-8")
            diff = "".join(
                difflib.unified_diff(
                    existing_text.splitlines(keepends=True),
                    [],
                    fromfile=f"a/{collection}/{path}",
                    tofile="/dev/null",
                )
            )
            self._record_preview(
                change_revision,
                {
                    "action": "delete",
                    "collection": collection,
                    "path": path,
                    "expected_revision": expected_revision,
                    "change_revision": change_revision,
                },
            )
            return {
                "action": "delete",
                "collection": collection,
                "path": path,
                "id": record["id"],
                "is_create": False,
                "expected_revision": expected_revision,
                "change_revision": change_revision,
                "approval_required": approval_required,
                "approval_template": self._approval_template(change_revision, approval_required),
                "policy_source": policy_source,
                "action_contract": self._action_contract(approval_required),
                "diff": diff,
            }

        if not isinstance(metadata, dict):
            raise ValueError("metadata is required for save preview")
        metadata = dict(metadata)
        metadata.pop("allow_similar", None)
        raw = render_markdown_document(metadata, content)
        parsed = parse_document(raw, location=path, module="core", collection=collection)
        if collection == "instructions":
            validate_instruction_document(parsed)
        elif metadata.get("type") == "instruction":
            raise ValueError("Instruction documents must be saved in the instructions collection")

        existing_text = target.read_text(encoding="utf-8") if target.exists() else ""
        actual_rev = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if expected_revision != actual_rev:
            raise ValueError("Document changed; read its current revision")

        approval_required, policy_source = self._approval_policy(collection, path, metadata)
        change_revision = self._change_revision({
            "action": "save",
            "collection": collection,
            "path": path,
            "metadata": metadata,
            "content": content,
            "expected_revision": expected_revision,
            "approval_required": approval_required,
        })
        diff = "".join(
            difflib.unified_diff(
                existing_text.splitlines(keepends=True),
                raw.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{collection}/{path}",
                tofile=f"b/{collection}/{path}",
            )
        )
        self._record_preview(
            change_revision,
            {
                "action": "save",
                "collection": collection,
                "path": path,
                "expected_revision": expected_revision,
                "change_revision": change_revision,
            },
        )
        result = {
            "action": "save",
            "collection": collection,
            "path": path,
            "id": metadata["id"],
            "is_create": actual_rev is None,
            "expected_revision": expected_revision,
            "change_revision": change_revision,
            "approval_required": approval_required,
            "approval_template": self._approval_template(change_revision, approval_required),
            "policy_source": policy_source,
            "action_contract": self._action_contract(approval_required),
            "diff": diff,
        }
        if actual_rev is None:
            result["similar"] = self._find_similar(collection, path, metadata)
        return result

    def save(
        self,
        collection: str,
        path: str,
        metadata: dict[str, Any],
        content: str,
        *,
        expected_revision: str | None = None,
        approval: Any = None,
    ) -> dict[str, Any]:
        """Persist a document after checking expected_revision, server preview receipt, and approval policy."""
        approval = self._normalize_approval(approval)
        validate_collection_name(collection)
        validate_relative_markdown_path(path)
        allow_similar = isinstance(metadata, dict) and metadata.get("allow_similar") is True
        if isinstance(metadata, dict):
            metadata = dict(metadata)
            metadata.pop("allow_similar", None)
        if collection != "instructions" and isinstance(metadata, dict) and metadata.get("type") == "instruction":
            raise ValueError("Instruction documents must be saved in the instructions collection")

        approval_required, policy_source = self._approval_policy(collection, path, metadata)
        expected_change_rev = self._change_revision({
            "action": "save",
            "collection": collection,
            "path": path,
            "metadata": metadata,
            "content": content,
            "expected_revision": expected_revision,
            "approval_required": approval_required,
        })
        if approval_required:
            if not (self._preview_dir() / f"{expected_change_rev}.json").is_file():
                raise ValueError("Preview required before save/delete; call core.documents_preview first and show the diff to the user")
            if not isinstance(approval, dict):
                raise ValueError(
                    'Approval is required by policy; call core.documents_preview, show diff, and pass approval={"change_revision": "<from preview>", "confirmation": "<user approval text>"}'
                )
            if approval.get("change_revision") != expected_change_rev:
                raise ValueError("Approval change_revision does not match current change and policy")
            conf = approval.get("confirmation")
            if not isinstance(conf, str) or not conf.strip():
                raise ValueError(
                    'Approval confirmation is required: pass approval={"change_revision": "<from preview>", "confirmation": "<non-empty user approval text>"}'
                )

        target = self.collections.store(collection)._resolve_target(path)
        is_create = expected_revision is None and not target.exists()
        similar = self._find_similar(collection, path, metadata) if is_create else []
        title = " ".join(str(metadata.get("title") or "").split()).casefold()
        if is_create and not allow_similar and any(
            (isinstance(item.get("distance"), (int, float)) and item["distance"] <= 0.22)
            or (title and title == " ".join(str(item.get("title") or "").split()).casefold())
            for item in similar
        ):
            return {
                "saved": False, "status": "similar_found", "collection": collection,
                "path": path, "similar": similar,
                "hint": "Similar card already exists in this context. Update the existing card by passing its path and expected_revision, or pass metadata={'allow_similar': True, ...} to force creating a separate card.",
            }
        res = self.collections.save(
            collection,
            path,
            metadata,
            content,
            expected_revision=expected_revision,
        )
        self._consume_preview(expected_change_rev)
        res["policy_source"] = policy_source
        res["change_revision"] = expected_change_rev
        if is_create:
            res["similar"] = similar
        return res

    def delete(
        self,
        collection: str,
        path: str,
        *,
        expected_revision: str,
        approval: Any = None,
    ) -> dict[str, Any]:
        """Delete a document if it has no incoming links and matches preview receipt and approval policy."""
        approval = self._normalize_approval(approval)
        validate_collection_name(collection)
        validate_relative_markdown_path(path)
        record = self.collections.store(collection).read(path)
        if record["revision"] != expected_revision:
            raise ValueError("Document changed; read its current revision")

        key = DocumentKey("core", collection, record["id"])
        relations = collect_relations(self.collections, key)
        if not relations["complete"]:
            raise ValueError("Cannot delete document while collection scan is incomplete")
        if relations["incoming"]:
            raise ValueError("Cannot delete document with incoming links")

        approval_required, policy_source = self._approval_policy(collection, path, record["metadata"])
        expected_change_rev = self._change_revision({
            "action": "delete",
            "collection": collection,
            "path": path,
            "expected_revision": expected_revision,
            "approval_required": approval_required,
        })
        if approval_required:
            if not (self._preview_dir() / f"{expected_change_rev}.json").is_file():
                raise ValueError("Preview required before save/delete; call core.documents_preview first and show the diff to the user")
            if not isinstance(approval, dict):
                raise ValueError(
                    'Approval is required by policy; call core.documents_preview, show diff, and pass approval={"change_revision": "<from preview>", "confirmation": "<user approval text>"}'
                )
            if approval.get("change_revision") != expected_change_rev:
                raise ValueError("Approval change_revision does not match delete request and policy")
            conf = approval.get("confirmation")
            if not isinstance(conf, str) or not conf.strip():
                raise ValueError(
                    'Approval confirmation is required: pass approval={"change_revision": "<from preview>", "confirmation": "<non-empty user approval text>"}'
                )

        res = self.collections.delete(collection, path, expected_revision=expected_revision)
        self._consume_preview(expected_change_rev)
        res["policy_source"] = policy_source
        return res
