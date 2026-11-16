"""Scoped Markdown storage for native and child modules."""

from dataclasses import asdict
from pathlib import Path

from ..module_storage.coordination import module_resource
from ..rag import DocumentKey, IndexDocument
from .file_store import DocumentFileStore
from .reconcile import SourceSnapshot, plan


class ModuleDocuments:
    def __init__(self, layout, module):
        self.layout, self.module = layout, module
        self.root = layout.module_data(module)

    def _path(self, relative, *, markdown=False):
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or ":" in relative or relative.startswith("/")):
            raise ValueError("Expected a relative module data path")
        parts = relative.split("/")
        if any(not p or p in {".", ".."} or p.startswith(".") for p in parts):
            raise ValueError("Invalid module data path")
        path = self.root.joinpath(*parts)
        for parent in (path, *path.parents):
            if parent == self.layout.root:
                break
            if parent.is_symlink():
                raise ValueError("Module document paths cannot contain symlinks")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Document escapes module data")
        if markdown and path.suffix != ".md":
            raise ValueError("Document path must end in .md")
        return path

    def _store(self, directory: str, collection: str | None = None) -> DocumentFileStore:
        root = self._path(directory)
        return DocumentFileStore(
            self.layout,
            module=self.module,
            collection=collection or directory,
            root=root,
            boundary_root=self.root,
        )

    @staticmethod
    def _wire(record: dict) -> dict:
        return {
            "key": record["key"],
            "revision": record["content_revision"],
            "text": record["content"],
            "location": record["path"],
            "metadata": record["metadata"],
            "file_revision": record["revision"],
            "path": record["full_path"],
        }

    def prepare(self, directory):
        with module_resource(self.layout, self.module, "documents"):
            path = self._path(directory)
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path)}

    def scan(self, directory, collection=None):
        store = self._store(directory, collection)
        snapshot = store.scan()
        documents = [self._wire(store._record_from_document(doc)) for doc in snapshot.documents]
        check = store.scan()
        if check != snapshot:
            raise ValueError("Module documents changed during scan; retry")
        return {"documents": documents, "complete": True}

    def write(self, directory, path, metadata, text, expected_revision=None, collection=None):
        store = self._store(directory, collection)
        lock = module_resource(self.layout, self.module, "documents")
        record = store.save(path, metadata, text, expected_revision=expected_revision, lock_ctx=lock)
        return self._wire(record)

    def delete(self, directory, path, expected_revision):
        lock = module_resource(self.layout, self.module, "documents")
        store = DocumentFileStore(
            self.layout,
            module=self.module,
            collection=directory,
            root=self._path(directory),
            boundary_root=self.root,
        )
        res = store.delete(path, expected_revision=expected_revision, lock_ctx=lock)
        return {"deleted": True, "path": res["full_path"]}

    def plan(self, collection, documents, previous, complete=False):
        def decode(items):
            result = []
            for item in items:
                key = item["key"]
                if key.get("module", self.module) != self.module:
                    raise PermissionError("Document scope is restricted to the calling module")
                result.append(IndexDocument(DocumentKey(self.module, key["collection"], key["id"]),
                    item["revision"], item.get("text", ""), item["location"], item.get("metadata", {})))
            return tuple(result)
        result = plan(SourceSnapshot(self.module, collection, decode(documents), complete), decode(previous))
        return {"upsert": [asdict(x) for x in result.upsert],
                "refresh": [asdict(x) for x in result.refresh],
                "delete": [asdict(x) for x in result.delete],
                "unchanged": [asdict(x) for x in result.unchanged]}
