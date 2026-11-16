"""Native FastMCP transport bound to one configured root and its modules."""

import atexit
import json
import os
import threading
from functools import partial
from typing import Any, Literal
import anyio
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from contextopus.core.delivery import CoreStructuredError, capture_delivery_manifest
from contextopus.core.operations import OperationInputError
from mcp.types import TextContent, ToolAnnotations
from starlette.responses import JSONResponse
from contextopus.startup import Application
from contextopus.core.resources import ProcessResources


class ServerOwner:
    def __init__(self, root):
        self.root = str(Path(root).expanduser().resolve())
        self.resources = ProcessResources()
        self.applications = {}
        self._lock = threading.RLock()

    def application(self, context=None):
        from contextopus.core.storage import StorageLayout

        if context is not None and (
            not isinstance(context, str) or not context.strip()
            or context in {'.', '..'} or '/' in context or '\\' in context
        ):
            raise ValueError('Use a context name, not a filesystem path')
        root = StorageLayout.resolve(self.root).root
        with self._lock:
            created = context not in self.applications
            if context not in self.applications:
                self.applications[context] = Application(
                    root, context, resources=self.resources
                )
            app = self.applications[context]
        if created and self.resources.services_started:
            self.resources.start_services()
        return app

    def current_root(self):
        return self.root

    def close(self):
        failures = []
        for app in list(self.applications.values()):
            try:
                app.close()
            except Exception as exc:
                failures.append(exc)
        self.applications.clear()
        try:
            self.resources.close()
        except Exception as exc:
            failures.append(exc)
        if failures:
            raise ExceptionGroup('MCP owner failed to close', failures)


class ContextopusMCP(FastMCP):
    async def call_tool(self, name, arguments):
        # FastMCP otherwise ignores extra fields. Reject stale clients before
        # they can execute a request against a different, server-owned root.
        if 'work_root' in arguments:
            raise ToolError('Storage is configured by the server. Remove work_root and use a context name.')
        return await super().call_tool(name, arguments)


def create_server(root, host="127.0.0.1", port=8765, *, legacy_tools=None):
    owner = ServerOwner(root)
    owner.application()
    owner.resources.start_services()
    mcp = ContextopusMCP(
        "Contextopus",
        host=host,
        port=port,
        instructions=(
            "Start Contextopus work with this server's guide tool: guide() returns the Contextopus base "
            "rules, the routes of enabled modules and the steps for every task; a skill may name a "
            "procedure directly with guide(procedure=...). Follow the returned rules. Tool names may "
            "carry a server prefix; use these tools, not similarly named local ones. If Contextopus MCP "
            "is unavailable, report the connection failure instead of bypassing it."
        ),
    )
    atexit.register(owner.close)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"root": owner.current_root(), "pid": os.getpid()})

    def reject_call(code, message, recovery):
        raise ToolError(json.dumps({"status": "error", "error": {
            "code": code, "message": message, "execution_started": False,
            "recovery": recovery,
        }}, ensure_ascii=False))

    def route(
        kind,
        operation,
        arguments,
        context=None,
        *,
        detail="compact",
        skip_presentation=False,
    ):
        app = owner.application(context)
        # Reaching transport dispatch proves this owner is serving requests.
        # It provides no evidence about a particular native client's identity.
        app.services.runtime_mcp["server"] = "running"
        try:
            normalized_op = app.normalize_operation(operation)
            op_arguments = (
                {"operation": app.normalize_operation(arguments["operation"])}
                if normalized_op == "describe" and isinstance(arguments, dict) and arguments.get("operation") is not None
                else arguments
            )
            with capture_delivery_manifest() as captured:
                result = getattr(app, kind)(normalized_op, op_arguments)
            if hasattr(app, "finalize_delivery"):
                return app.finalize_delivery(
                    result,
                    operation=normalized_op,
                    kind=kind,
                    arguments=op_arguments,
                    detail=detail,
                    delivery_manifest=captured.manifest,
                    skip_presentation=skip_presentation,
                )
            if normalized_op == "core.page" or skip_presentation:
                return result
            from .presentation import present
            return present(result, operation=normalized_op, arguments=op_arguments, detail=detail)
        except (OperationInputError, CoreStructuredError) as exc:
            # Preserve MCP isError, including for guide and legacy entrypoints.
            raise ToolError(json.dumps(exc.payload, ensure_ascii=False)) from None

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def guide(
        procedure: str | None = None,
        operation: str | None = None,
        catalog: bool = False,
        context: str | None = None,
    ) -> Any:
        """guide() is the Contextopus entry: Core base rules, routes of enabled modules and steps for every task. procedure=<name> returns that procedure with the base rules, steps and compact operation catalog; procedure=<module> lists its procedures. operation=... gives only a signature, not a replacement for a procedure. Oversized responses paginate via read(operation='core.page', arguments={'response_id':..., 'part':...}). Catalog is opt-in."""
        if sum((procedure is not None, operation is not None, catalog)) > 1:
            reject_call('conflicting_guide_selectors', 'Choose procedure, operation or catalog', 'Request exactly one procedure, one operation signature, or catalog=true.')
        if operation is not None or catalog:
            args = {'operation': operation} if operation is not None else {}
            result = await anyio.to_thread.run_sync(
                partial(route, 'query', 'describe', args, context, detail='compact', skip_presentation=True)
            )
        else:
            result = await anyio.to_thread.run_sync(
                partial(route, 'query', 'procedure', {'name': procedure or 'default'}, context, detail='compact', skip_presentation=True)
            )
        if isinstance(result, dict):
            return result
        return [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]

    async def dispatch(kind, operation, arguments, context, detail):
        # Validate before executing a mutation. Presentation and pagination run only
        # after the existing registry has completed approval checks and observation.
        if detail not in ('compact', 'full'):
            reject_call('invalid_detail', 'detail must be compact or full', 'Use compact normally; full only when needed evidence is missing.')
        result = await anyio.to_thread.run_sync(
            partial(route, kind, operation, arguments, context, detail=detail, skip_presentation=False)
        )
        if isinstance(result, dict):
            return result
        # FastMCP otherwise expands a list into separate content blocks, losing
        # its JSON shape (and making empty/multiple results undecodable).
        return [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def read(
        operation: str, arguments: dict | None = None,
        context: str | None = None,
        detail: Literal['compact', 'full'] = 'compact',
    ) -> Any:
        """Run a query; compact metadata is standard, full detail preserves all evidence."""
        return await dispatch('query', operation, arguments, context, detail)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    async def write(
        operation: str, arguments: dict | None = None,
        context: str | None = None,
        detail: Literal['compact', 'full'] = 'compact',
    ) -> Any:
        """Run an authorized mutation under its existing review and approval policy."""
        return await dispatch('execute', operation, arguments, context, detail)

    # Explicit temporary compatibility for old clients. It preserves full legacy
    # responses and is never advertised by the default server.
    if legacy_tools is None:
        legacy_tools = os.environ.get('CONTEXTOPUS_LEGACY_TOOLS') == '1'
    if legacy_tools:
        async def legacy_query(operation: str, arguments: dict | None = None,
                               context: str | None = None) -> Any:
            return await dispatch('query', operation, arguments, context, 'full')
        async def legacy_execute(operation: str, arguments: dict | None = None,
                                 context: str | None = None) -> Any:
            return await dispatch('execute', operation, arguments, context, 'full')
        for name in ('contextopus_query', 'pcw_query'):
            mcp.tool(name=name, annotations=ToolAnnotations(readOnlyHint=True))(legacy_query)
        for name in ('contextopus_execute', 'pcw_execute'):
            mcp.tool(name=name, annotations=ToolAnnotations(readOnlyHint=False))(legacy_execute)
    return mcp


def run(root, transport="streamable-http", host="127.0.0.1", port=8765):
    import json
    from contextopus.core import locking
    from contextopus.core.storage import StorageLayout
    root = StorageLayout.resolve(root).root
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "mcp-owner.lock").open("a") as owner_lock:
        # Every launch route, including native OS services, shares one ownership guard.
        locking.flock(owner_lock, locking.LOCK_EX | locking.LOCK_NB)
        (runtime / "service.json").write_text(json.dumps({"pid": os.getpid(), "root": str(root)}))
        create_server(root, host=host, port=port).run(transport=transport)
