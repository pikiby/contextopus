"""File-authoritative shared instruction library; delegates storage to DocumentCollections."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .collections import DocumentCollections, validate_instruction_document
from .files import parse_document
from .links import resolve_required
from .reconcile import SourceSnapshot


class InstructionLibrary:
    """Own Markdown/YAML instruction documents in ``root/core/instructions``."""

    def __init__(self, layout, index=None, *, collections: DocumentCollections | None = None):
        self.layout = layout
        self.index = index
        self.collections = collections or DocumentCollections(layout, index)
        self.root = self.collections.instructions_root

    @staticmethod
    def _validate(document) -> None:
        validate_instruction_document(document)

    def snapshot(self) -> SourceSnapshot:
        """Capture validated instruction sources for a shared-index rebuild."""
        return self.collections.snapshot("instructions")

    def read(self, reference: str) -> dict[str, Any]:
        """Read a complete instruction body and full-file revision by exact ID or wikilink."""
        record = self.collections.read("instructions", reference)
        return {
            "id": record["id"],
            "type": "instruction",
            "title": record["title"],
            "description": record["description"],
            "path": record["path"],
            "content": record["content"],
            "revision": record["revision"],
            "metadata": record["metadata"],
        }

    def list(self) -> dict[str, Any]:
        """List all validated instruction records."""
        records = [
            {
                "id": r["id"],
                "type": "instruction",
                "title": r["title"],
                "description": r["description"],
                "path": r["path"],
                "content": r["content"],
                "revision": r["revision"],
                "metadata": r["metadata"],
            }
            for r in self.collections.list("instructions")
        ]
        return {"instructions": records}

    def resolve(self, references, prepared=None) -> dict[str, Any]:
        """Resolve exact required rule links recursively, dependencies first and once each."""
        if not isinstance(references, (list, tuple)):
            raise ValueError("references must be a list")
        cleaned_refs = []
        for reference in references:
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("Expected an instruction reference")
            cleaned = reference.strip()
            cleaned_refs.append(cleaned if cleaned.startswith("[[") else f"[[{cleaned}]]")

        documents = list(self.snapshot().documents)
        pending: dict[Any, dict[str, Any]] = {}
        for item in prepared or []:
            document = parse_document(
                item["text"].encode("utf-8"),
                location=item["path"],
                module="core",
                collection="instructions",
            )
            self._validate(document)
            matches = [d for d in documents if d.key == document.key or d.location == document.location]
            if matches:
                existing = self.collections.store("instructions")._record_from_document(matches[0])
                if (
                    len(matches) != 1
                    or matches[0].key != document.key
                    or matches[0].location != document.location
                    or existing["revision"] != item["revision"]
                ):
                    raise ValueError("Prepared instruction conflicts with an existing document")
            else:
                documents.append(document)
                pending[document.key] = {k: v for k, v in item.items() if k != "text"}

        ordered = resolve_required(documents, cleaned_refs)
        records = []
        for document in ordered:
            if document.key in pending:
                records.append(pending[document.key])
            else:
                rec = self.collections.store("instructions")._record_from_document(document)
                records.append({
                    "id": rec["id"],
                    "type": "instruction",
                    "title": rec["title"],
                    "description": rec["description"],
                    "path": rec["path"],
                    "content": rec["content"],
                    "revision": rec["revision"],
                    "metadata": rec["metadata"],
                })

        revisions = [[r["id"], r["path"], r["revision"]] for r in records]
        revision = hashlib.sha256(
            json.dumps(revisions, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {"instructions": records, "revision": revision}
