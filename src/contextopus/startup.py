"""Application assembly: explicit root/context, shared resources and module registry."""

import hashlib
import json
from dataclasses import dataclass
from importlib import metadata, resources as package_resources
from pathlib import Path, PurePosixPath
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, create_model
from .core.operations import OperationRegistry, OperationSpec
from .core.resources import ProcessResources
from .core.storage import StorageLayout
from .core.monitoring import RootServices


class DocumentApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    change_revision: str = ""
    confirmation: str = ""


@dataclass(frozen=True)
class RequestContext:
    root: Path
    context: str | None = None


class Application:
    def __init__(self, root, context=None, *, resources=None, discover=True):
        self.layout = StorageLayout.resolve(root)
        self.context = RequestContext(self.layout.root, context)
        self.resources = resources or ProcessResources()
        self._owns_resources = resources is None
        from .core.analytics import OperationJournal

        self.journal = OperationJournal(self.layout)
        self.registry = OperationRegistry(self.journal)
        from .core.rag import RagIndex

        self.index = RagIndex(self.layout, self.resources)
        from .core.documents.collections import DocumentCollections
        from .core.documents.contexts import ContextReader
        from .core.documents.instructions import InstructionLibrary
        from .core.documents.mutations import DocumentMutations

        self.collections = DocumentCollections(self.layout, self.index)
        self.instructions = InstructionLibrary(
            self.layout, self.index, collections=self.collections
        )
        from .core.module_storage import ModuleStorage
        from .core.response_store import ResponseStore
        self.module_storage = ModuleStorage(self.layout)
        self.services = RootServices(self.layout, self.resources, self.registry)
        self.contexts = ContextReader(
            self.collections, self.instructions, settings=self.services._settings
        )
        self.mutations = DocumentMutations(
            self.collections, settings=self.services._settings
        )
        self.delivery_handlers = {}
        self.response_store = ResponseStore(
            self.layout,
            delivery_dispatcher=self.deliver_receipt,
        )
        self._register_core()
        if discover:
            for entry in metadata.entry_points(group="contextopus.modules"):
                before = dict(self.registry._operations), dict(self.registry.modules)
                try:
                    entry.load()(self)
                except Exception as exc:
                    self.registry._operations, self.registry.modules = before
                    self.registry.register_module(entry.name, available=False,
                                                  state="error", error=str(exc))
        from .core.module_management import ModuleManager
        self.module_manager = ModuleManager(self)
        self._operation("core.modules", "query", self.module_manager.discover)
        for action in ("prepare", "enable", "disable"):
            self._operation("core.module_" + action, "execute", getattr(self.module_manager, action), {"module": (str, ...)})
        self._operation("core.module_source_plan", "query", self.module_manager.source_plan,
                        {"module": (str, ...), "source": (str, ...)})
        self._operation("core.module_install", "execute", self.module_manager.install,
                        {"module": (str, ...), "source": (str | None, None), "source_revision": (str | None, None)})
        self._operation("core.module_update", "execute", self.module_manager.update, {"module": (str, ...), "source": (str, ...)})
        self._refresh_algorithms()
        self._register_search_tools()

    def _operation(self, name, kind, fn, fields=None):
        model = create_model(
            name.replace(".", "_") + "_arguments",
            __config__=ConfigDict(extra="forbid", strict=True),
            **(fields or {}),
        )
        self.registry.register(
            OperationSpec(
                name,
                kind,
                model,
                lambda request_context, **kw: fn(**kw),
                description=fn.__doc__ or name.replace("_", " ").replace(".", " "),
                guard_root=not (name.startswith("core.relocation_") or name.startswith("core.module_")
                    or name in {
                        "core.context_read",
                        "core.documents_preview",
                        "core.documents_relations",
                        "core.instructions_list",
                        "core.instructions_read",
                        "core.instructions_resolve",
                        "core.library",
                        "core.page",
                    }),
            )
        )

    def _register_core(self):
        from .core.documents.library import browse_documents
        from .core.documents.links import collect_relations
        from .core.rag import DocumentKey

        self._operation("core.library", "query",
                        lambda **kw: browse_documents(self.collections, **kw),
                        {"action": (str, ...), "collection": (str, ""),
                         "query": (str, ""), "mode": (str, "text"), "type": (str, ""),
                         "offset": (Annotated[int, Field(ge=0, le=100000)], 0),
                         "limit": (Annotated[int, Field(ge=1, le=100)], 40), "id": (str, "")})
        self._operation("core.context_read", "query", self.context_read,
                        {"id": (str | None, None),
                         "cwd": (str | None, None),
                         "expected_revision": (str | None, None)})
        self._operation("core.documents_preview", "query", self.mutations.preview,
                        {"collection": (str, ...), "path": (str, ...),
                         "metadata": (dict | None, None), "content": (str, ""),
                         "expected_revision": (str | None, None),
                         "action": (str, "save")})
        self._operation("core.documents_save", "execute", self.mutations.save,
                        {"collection": (str, ...), "path": (str, ...),
                         "metadata": (dict, ...), "content": (str, ...),
                         "expected_revision": (str | None, None),
                         "approval": (DocumentApprovalInput | None, None)})
        self._operation("core.documents_delete", "execute", self.mutations.delete,
                        {"collection": (str, ...), "path": (str, ...),
                         "expected_revision": (str | None, None),
                         "approval": (DocumentApprovalInput | None, None)})
        self._operation("core.documents_reconcile", "execute", self.collections.reconcile,
                        {"collection": (str | None, None)})
        self._operation("core.documents_relations", "query",
                        lambda collection, id: collect_relations(
                            self.collections, DocumentKey("core", collection, id)
                        ),
                        {"collection": (str, ...), "id": (str, ...)})
        self._operation("core.instructions_list", "query", self.instructions.list)
        self._operation("core.instructions_read", "query", self.instructions.read,
                        {"reference": (str, ...)})
        self._operation("core.instructions_resolve", "query", self.instructions.resolve,
                        {"references": (list[str], ...)})
        self._operation("core.status", "query", self.services.status)
        self._operation(
            "core.search",
            "query",
            self.search,
            {
                "query": (str, ...),
                "module": (str | None, None),
                "collection": (str | None, None),
                "scopes": (list[dict] | None, None),
                "settings_context": (str | None, None),
                "object_type": (str | None, None),
                "status": (str | None, None),
                "include_archived": (bool | None, None),
                "distance": (float | None, None),
                "limit": (Annotated[int, Field(ge=1, le=1000)] | None, None),
                "selection": (dict | None, None),
                "output": (str, "materials"),
                "candidate_limit": (Annotated[int, Field(ge=1, le=100000)] | None, None),
                "purpose": (str | None, None),
                "task_id": (str | None, None),
            },
        )
        self._operation("core.model_hardware", "query", self.model_hardware)
        self._operation(
            "core.model_plan",
            "query",
            self.model_plan,
            {
                "config": (dict | None, None),
                "chunking": (dict | None, None),
                "device": (str, "auto"),
            },
        )
        self._operation(
            "core.model_apply", "execute", self.model_apply, {"plan": (dict, ...)}
        )
        self._operation(
            "core.settings",
            "query",
            self.settings,
            {
                "module": (str | None, None),
                "context": (str | None, None),
                "request": (dict | None, None),
                "fields": (list[str] | None, None),
                "agent": (str | None, None),
                "agent_model": (str | None, None),
            },
        )
        self._operation(
            "core.settings_preview",
            "query",
            self.settings_preview,
            {
                "changes": (dict, ...),
                "module": (str | None, None),
                "context": (str | None, None),
                "request_only": (bool, False),
            },
        )
        self._operation(
            "core.settings_apply",
            "execute",
            self.settings_apply,
            {"preview_revision": (str, ...), "confirmation": (str | None, None)},
        )
        self._operation("procedure", "query", self.procedure, {"name": (str, ...)})
        self._operation("core.page", "query", self.response_page, {
            "response_id": (str, Field(pattern=r"^[0-9a-f]{32}$")),
            "part": (int, Field(ge=1)),
        })
        self._operation("core.init", "execute", self.initialize)
        self._operation("core.doctor", "query", self.doctor)
        self._operation("core.index_upgrade", "execute", self.index_upgrade)
        self._operation(
            "core.relocation_plan",
            "query",
            self.relocation_plan,
            {"destination": (str, ...), "include_manifest": (bool, False)},
        )
        self._operation(
            "core.relocation_apply",
            "execute",
            self.relocation_apply,
            {"plan": (dict | None, None), "plan_revision": (str | None, None)},
        )
        self._operation(
            "core.relocation_rollback",
            "execute",
            self.relocation_rollback,
            {"plan": (dict | None, None), "plan_revision": (str | None, None)},
        )

    def _resolve_settings_scope(self, module=None, context=None):
        if context is None and module is None and self.context.context:
            context = self.context.context
        if context is not None and module in (None, "core"):
            try:
                context = self.collections.read("contexts", context)["id"]
            except (FileNotFoundError, ValueError):
                pass
            module = "core"
        return module, context

    def settings(self, module=None, context=None, request=None, fields=None, agent=None, agent_model=None):
        module, context = self._resolve_settings_scope(module, context)
        if fields is None:
            self._refresh_algorithms()
        return self.services._settings.snapshot(module, context, request, fields=fields, agent=agent, agent_model=agent_model)

    def settings_preview(self, changes, module=None, context=None, request_only=False):
        module, context = self._resolve_settings_scope(module, context)
        self._refresh_algorithms()
        return self.services._settings.preview(changes, module, context, request_only)

    def context_read(self, id: str | None = None, expected_revision: str | None = None, cwd: str | None = None):
        target_id = id if id is not None else (None if cwd else self.context.context)
        if not target_id and not cwd:
            raise ValueError("Context id is required")
        return self.contexts.read(target_id, expected_revision=expected_revision, cwd=cwd)

    def settings_apply(self, preview_revision, confirmation=None):
        self._refresh_algorithms()
        return self.services.apply_settings(preview_revision, confirmation=confirmation)

    def _refresh_algorithms(self):
        from .core.filtering import registry_for_layout
        registry = registry_for_layout(self.layout)
        self.index.algorithm_registry = registry
        self.services._settings.registry = registry

    def _register_search_tools(self):
        from .core.search_history import SearchHistory
        history = SearchHistory(self.journal, self.index.algorithm_registry)
        self._operation("core.search_events", "query", history.query, {
            "since": (str | None, None), "until": (str | None, None),
            "context": (str | None, None), "task_id": (str | None, None),
            "instructions_revision": (str | None, None),
            "offset": (Annotated[int, Field(ge=0)], 0),
            "limit": (Annotated[int, Field(ge=1, le=100)], 50)})
        self._operation("core.search_event", "query", history.read, {"event_id": (str, ...)})
        def replay(event_id, selection, relevant_keys=None):
            self._refresh_algorithms()
            return SearchHistory(self.journal, self.index.algorithm_registry).replay(event_id, selection, relevant_keys)
        self._operation("core.search_replay", "query", replay, {
            "event_id": (str, ...), "selection": (dict, ...), "relevant_keys": (list | None, None)})

        def catalog(query=None, semantic=False, limit=20):
            from .core.filtering import AlgorithmCatalog
            self._refresh_algorithms()
            return AlgorithmCatalog(self.index.algorithm_registry, self.index).search(query, limit, semantic)
        def refresh():
            from .core.filtering import AlgorithmCatalog
            self._refresh_algorithms()
            return AlgorithmCatalog(self.index.algorithm_registry, self.index).refresh()
        self._operation("core.algorithms", "query", catalog, {
            "query": (str | None, None), "semantic": (bool, False), "limit": (int, 20)})
        self._operation("core.algorithms_refresh", "execute", refresh)
        self._operation("core.index_stats", "query", self.index_stats, {
            "module": (str | None, None), "collection": (str | None, None)})

    def index_stats(self, module=None, collection=None):
        """Count stored fragments and unique objects without retrieving vectors."""
        table = self.index._generic_table()
        if table is None:
            return {"fragments": 0, "objects": 0, "module": module, "collection": collection}
        clauses = [name + "=" + self.index._quote(value) for name, value in
                   (("module", module), ("collection", collection)) if value is not None]
        rows = self.index.read_rows(table, ["module", "collection", "object_id"], " AND ".join(clauses) or None)
        return {"fragments": len(rows), "objects": len({(r["module"], r["collection"], r["object_id"]) for r in rows}),
                "module": module, "collection": collection, "basis": "stored_index_rows"}

    def active_profile(self):
        import json

        path = self.layout.rag / "active.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def model_hardware(self):
        from .core.model_setup import detect_hardware

        return detect_hardware()

    def model_plan(self, config=None, chunking=None, device="auto"):
        from .core.model_setup import plan_embedding_setup

        return plan_embedding_setup(
            config, chunking, device, previous=self.active_profile()
        )

    def model_apply(self, plan):
        from .core.model_setup import apply

        result = apply(
            plan, apply_profile=self.apply_profile, read_active=self.active_profile
        )
        if result.get("status") == "ok":
            result["setup"] = self.module_manager.run_hooks('setup')
        return result

    def apply_profile(self, config, chunking):
        owners = sum(len(host.hooks.get('apply_profile', []))
                     for host in self.module_manager.contributions.values())
        if owners > 1:
            raise ValueError('Multiple modules contribute profile rebuilding; select one owner before changing the profile')
        results = self.module_manager.run_hooks('apply_profile', config, chunking)
        applied = [result for values in results.values() for result in values if result is not None]
        if len(applied) > 1:
            raise ValueError('Multiple modules attempted to own profile rebuilding')
        if applied:
            return applied[0]
        active = self.active_profile()
        if active:
            from .core.rag import rebuild_profile

            self.index._generic_table()
            adapters = self.index.source_adapters
            unowned = self.index.read_rows(
                self.index.table,
                ["module", "collection"],
                "NOT (module = 'core' AND collection = 'search_algorithm')",
            )
            missing_owners = sorted({r["module"] for r in unowned if r["module"] not in adapters})
            if missing_owners:
                raise ValueError(
                    "Profile switch requires source adapters for existing documents: "
                    + ", ".join(missing_owners)
                )
            core_collections = {row["collection"] for row in unowned if row["module"] == "core"}
            if any(not self.collections.collection_root(collection).is_dir()
                   for collection in core_collections):
                raise ValueError("Core document directory is unavailable; cannot rebuild its index")

            snapshots = {
                mod_name: adapter.snapshots()
                for mod_name, adapter in adapters.items()
                if hasattr(adapter, "snapshots")
            }
            all_documents = [
                doc
                for mod_snaps in snapshots.values()
                for snap in mod_snaps
                for doc in snap.documents
            ]

            def validate_sources():
                current = {
                    mod_name: adapters[mod_name].snapshots()
                    for mod_name in snapshots
                }
                if current != snapshots:
                    raise ValueError("Documents changed during profile rebuild; retry")

            result = rebuild_profile(
                self.index,
                previous=active,
                config=config,
                search=active["search"],
                chunking=chunking,
                documents=all_documents,
                validate_snapshot=validate_sources,
                read_active=self.active_profile,
                write_active=self.write_active_profile,
                factory=self.resources.acquire,
            )
            provider = result.pop("_provider", None)
            if provider is not None:
                provider.close()
            self.index._provider = None
            self.index.embeddings = self.index.chunking = None
            return result
        # Initial setup verifies the actual adapter before publishing a pointer.
        from .core.rag import RagIndex

        provider = self.resources.acquire(config)
        try:
            RagIndex.vectors(
                provider,
                ["Embedding connection verification"],
                config["profile"]["dimensions"],
                "document",
            )
            self.initialize()
            self.index.embeddings, self.index.chunking = config, chunking
            self.index._generic_table(create=True)
            self.index._provider = provider
        except Exception:
            provider.close()
            raise
        return {"status": "ok", "active": self.active_profile(), "reembedded": False}

    def write_active_profile(self, active):
        import json, os
        from .core.storage import sync_directory

        path = self.layout.rag / "active.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(active, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)

    def search(self, query, **options):
        self._refresh_algorithms()
        root_values = self.services.settings()["values"]
        scopes = options.get("scopes")
        if scopes is None and self.context.context and not options.get("collection") and not options.get("module"):
            context_record = self.collections.read("contexts", self.context.context)
            metadata = context_record.get("metadata") or {}
            configured = metadata.get("search_scope")
            path = PurePosixPath(context_record["path"])
            slug = metadata.get("slug") or (path.parent.as_posix() if path.name == "Context.md" else path.stem)
            scopes = list(configured) if isinstance(configured, list) else [
                {"module": "core", "collection": "notes", "prefix": f"{slug}/"}
            ]
            for collection in ("skills", "profiles", "teams", "session_summaries"):
                if not any(isinstance(scope, dict) and scope.get("module") == "core"
                           and scope.get("collection") == collection for scope in scopes):
                    scopes.append({"module": "core", "collection": collection})
            options["scopes"] = scopes
        selected_module = (
            options.get("module")
            or root_values.get("module")
            or (next(iter(self.index.source_adapters))
                if not scopes and self.context.context and len(self.index.source_adapters) == 1 else None)
        )
        selected_collection = (
            options.get("collection")
            or (root_values.get("context") if not scopes else None)
        )
        effective_settings_context = (
            options.get("settings_context")
            or self.context.context
            or (selected_collection if selected_module else None)
        )
        settings_module = selected_module or ("core" if effective_settings_context is not None else None)
        snapshot = self.services._settings.snapshot(
            module=settings_module,
            context=effective_settings_context,
        )
        values = snapshot["values"]
        defaults = {
            "module": selected_module if scopes is None else options.get("module"),
            "collection": selected_collection if scopes is None else options.get("collection"),
            "settings_context": effective_settings_context,
            "object_type": values.get("object_type"),
            "include_archived": values.get("include_archived", False),
        }
        defaults.update({k: v for k, v in options.items() if v is not None})
        result = self.index.search_detailed(query, **defaults)
        parameters = result["diagnostics"]["effective_settings"]["parameters"]
        effective = {**defaults, "limit": next((parameters[name] for name in
                     ("N", "page_size", "top_k") if name in parameters), None)}
        return {**result, "effective": effective}

    def _procedure_directory(self, module=None):
        """Discover help without invoking module providers or loading their bodies."""
        rows = {}
        for name, (owner, provider) in self.module_manager.procedures.items():
            if module is None or owner == module:
                rows[name] = {"name": name, "description": "Registered procedure: " + name}
        modules = sorted(self.module_manager.loaded)
        for owner in modules:
            if module is not None and owner != module:
                continue
            base = self.module_manager.sources(owner, "procedures")
            if not base.is_dir():
                continue
            for candidate in sorted(base.rglob("*.md")):
                relative = candidate.relative_to(base)
                if any(part.startswith('.') for part in relative.parts):
                    continue
                path = self.module_manager._inside(base, str(relative))
                name = owner + "/" + relative.with_suffix("").as_posix()
                with path.open(encoding="utf-8") as stream:
                    heading = next((line.strip().lstrip('#').strip() for line in stream
                                    if line.startswith('# ')), name)
                rows.setdefault(name, {"name": name, "description": heading})
        if module is None:
            base = package_resources.files("contextopus").joinpath("resources", "procedures")
            def collect(directory, prefix=""):
                for path in directory.iterdir():
                    if path.name.startswith('.'):
                        continue
                    if path.is_dir():
                        collect(path, prefix + path.name + "/")
                    elif path.name.endswith('.md'):
                        name = prefix + path.name[:-3]
                        heading = next((line.lstrip('#').strip() for line in
                                        path.read_text(encoding="utf-8").splitlines()
                                        if line.startswith('# ')), name)
                        rows.setdefault(name, {"name": name, "description": heading})
            collect(base)
        return {
            "procedures": [rows[key] for key in sorted(rows)],
            "modules": modules if module is None else [module],
            "instruction": "This is a directory, not an action procedure. Before making changes, "
                "fetch and read the applicable procedure with guide(procedure=<exact name>). "
                "An operation signature does not replace the procedure. Do not proceed with "
                "changes until the applicable procedure has been obtained.",
        }

    def procedure(self, name):
        """guide(): the Core router for 'default', otherwise one procedure with the base rules."""
        from .core import router
        from .core.procedure_response import enrich_procedure
        if name == "default":
            self.module_manager.refresh()
            return router.router_procedure(self._base_rules(), self._router_entries())
        result = enrich_procedure(self._procedure_text(name), self.registry, self.normalize_operation)
        if not isinstance(result.get("content"), str):
            return result
        return router.attach(result, self._base_rules(), self._router_entries(), name)

    def _base_rules(self):
        from .core.router import BASE_RULES
        return package_resources.files("contextopus").joinpath(
            "resources", BASE_RULES).read_text(encoding="utf-8")

    def _router_entries(self):
        from .core.router import entries
        manifests = {module: self.module_manager.loaded[module] for module in self.module_manager.loaded}
        return entries(manifests, self.services._settings.router_overrides())

    def _procedure_text(self, name):
        parts = name.split("/")
        if any(not p or p in {".", ".."} or "\\" in p for p in parts):
            raise ValueError("Invalid procedure name")
        self.module_manager.refresh()
        if name in self.module_manager.procedures:
            return self.module_manager.procedures[name][1]()
        if name in self.module_manager.loaded:
            return {"name": name, "status": "directory", **self._procedure_directory(name)}
        if "/" in name:
            module, relative = name.split("/", 1)
            if module in self.module_manager.loaded:
                base = self.module_manager.sources(module, "procedures")
                path = self.module_manager._inside(base, relative + ".md")
                if path.is_file():
                    return {"name": name, "content": path.read_text(encoding="utf-8")}
                return {"name": name, "status": "not_found",
                        "error": "Unknown procedure: " + name,
                        **self._procedure_directory(module)}
        parts = name.split("/")
        path = package_resources.files("contextopus").joinpath(
            "resources", "procedures", *parts[:-1], parts[-1] + ".md"
        )
        if not path.is_file():
            return {"name": name, "status": "not_found",
                    "error": "Unknown procedure: " + name, **self._procedure_directory()}
        return {"name": name, "content": path.read_text(encoding="utf-8")}

    def index_upgrade(self):
        if self.active_profile() is None:
            return {"status": "not_initialized", "root": str(self.layout.root)}
        self.index._generic_table(create=True)
        return {
            "status": "upgraded",
            "table": self.index.table.name,
            "columns": self.index.table.schema.names,
            "model_loaded": self.index._provider is not None,
        }

    def initialize(self):
        if (self.layout.root / ".pcw").is_dir():
            return {
                "status": "relocation_required",
                "root": str(self.layout.root),
                "next_action": "core.relocation_plan",
            }
        with self.layout.guard():
            for path in (
                self.layout.rag,
                self.layout.analytics,
                self.layout.configuration,
            ):
                path.mkdir(parents=True, exist_ok=True)
        self.module_manager.run_hooks('initialize')
        return {"status": "initialized", "root": str(self.layout.root),
                "setup": self.module_manager.run_hooks('setup')}

    def doctor(self):
        return {
            "root": str(self.layout.root),
            "exists": self.layout.root.is_dir(),
            "frozen": (self.layout.root / ".contextopus-freeze.json").exists(),
            "layout": "legacy"
            if (self.layout.root / ".pcw").exists()
            else "contextopus",
            "model_loaded": bool(self.resources.providers._entries),
        }

    def relocation_plan(self, destination, include_manifest=False):
        from .core.relocation import RelocationPlans

        return RelocationPlans(self.layout.root).review(destination, include_manifest)

    def relocation_apply(self, plan=None, plan_revision=None):
        from .core.module_management.lifecycle import module_lifecycle
        # Lifecycle -> runtime -> root: children finish their Core RPC before
        # relocation takes the filesystem guard and publishes the freeze.
        with module_lifecycle(self.layout.root), self.module_manager.runtime.lock:
            from .core.relocation import apply, RelocationPlans

            plan = RelocationPlans(self.layout.root).resolve(plan, plan_revision)
            self.module_manager.runtime.close()
            with self.module_manager.quiesce_contributions():
                self.module_manager.run_hooks('before_relocation')
                self.index.table = self.index.db = None
                result = apply(plan)
                self.journal.rebind(StorageLayout(result["destination"]))
                return result

    def relocation_rollback(self, plan=None, plan_revision=None):
        from .core.relocation import rollback, RelocationPlans

        plan = RelocationPlans(self.layout.root).resolve(plan, plan_revision)
        result = rollback(plan)
        return result

    def query(self, operation, arguments=None):
        operation = self.normalize_operation(operation)
        if operation == "describe":
            selected = (arguments or {}).get("operation")
            return self.registry.describe(self.normalize_operation(selected) if selected is not None else None)
        return self.registry.dispatch("query", operation, arguments, self.context)

    def execute(self, operation, arguments=None):
        return self.registry.dispatch("execute", self.normalize_operation(operation), arguments, self.context)

    def deliver_receipt(self, receipt):
        """Dispatch a page delivery receipt to the owning module handler."""
        from .core.delivery import dispatch_delivery_receipt

        if receipt.owner not in self.delivery_handlers:
            self.module_manager.refresh()
        return dispatch_delivery_receipt(
            self.delivery_handlers,
            receipt,
            operation=receipt.operation or "core.page",
            execution_started=False,
        )

    def response_page(self, response_id, part):
        """Fetch and record delivery for one page of a previously paginated response."""
        self.module_manager.refresh()
        return self.response_store.fetch_page(
            response_id,
            part,
            expected_context=self.context.context,
            delivery_handlers=self.delivery_handlers,
        )

    def finalize_delivery(
        self,
        result,
        *,
        operation,
        kind,
        arguments=None,
        detail="compact",
        delivery_manifest=None,
        skip_presentation=False,
        present_fn=None,
    ):
        """Present, enforce response size limits, paginate if needed and notify module delivery."""
        from .core.response_store import finalize_operation_delivery

        if not skip_presentation and present_fn is None:
            from .interfaces.mcp.presentation import present as present_fn
        if delivery_manifest is not None and delivery_manifest.owner not in self.delivery_handlers:
            self.module_manager.refresh()
        return finalize_operation_delivery(
            result,
            operation=operation,
            kind=kind,
            context=self.context.context,
            response_store=self.response_store,
            delivery_handlers=self.delivery_handlers,
            delivery_manifest=delivery_manifest,
            arguments=arguments,
            detail=detail,
            skip_presentation=skip_presentation,
            present_fn=present_fn,
        )

    def normalize_operation(self, operation):
        self.module_manager.refresh()
        return self.module_manager.aliases.get(operation, (None, operation))[1]

    def close(self):
        failures = []
        try:
            self.module_manager.close_contributions()
        except Exception as exc:
            failures.append(exc)
        if self._owns_resources:
            try:
                self.resources.close()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup('Application failed to close', failures)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
