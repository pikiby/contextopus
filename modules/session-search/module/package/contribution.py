"""Native facade contribution using only generic Core module capabilities."""
from typing import Annotated
from pydantic import Field
from .session_search.fast_index import DialogueIndex


def contribute(core):
    storage = core.module_storage.for_module(core.module).native()
    contract = {'engine': 'sqlite', 'schema_version': 2, 'migrations': 'migrations',
                'compatible_versions': {'min': 2, 'max': 2}}
    core.hook('install_prepare', lambda: storage.prepare_schema(contract, core.package))
    core.hook('install_apply', lambda revision: storage.apply_schema(contract, core.package, revision))
    core.hook('ready', lambda: storage.prepare_schema(contract, core.package))
    runtime = DialogueIndex(storage)
    optional = (str | None, None)
    limit = (Annotated[int, Field(ge=1, le=100)], 20)
    core._operation('session_search.status', 'query', runtime.status)
    core._operation('session_search.configure', 'execute', runtime.configure, {
        'source': (str, ...), 'enabled': (bool | None, None), 'root': optional})
    core._operation('session_search.scan', 'execute', runtime.scan, {
        'source': optional, 'max_bytes': (Annotated[int, Field(ge=1024, le=67108864)], 8388608)})
    core._operation('session_search.list', 'query', runtime.list, {
        'source': optional, 'cwd': optional, 'since': optional, 'limit': limit})
    core._operation('session_search.find', 'query', runtime.find, {
        'query': (Annotated[str, Field(min_length=1, max_length=2000)], ...), 'source': optional, 'limit': limit})
    core._operation('session_search.read', 'query', runtime.read, {
        'session_id': (str, ...), 'source': optional, 'start_part': (Annotated[int, Field(ge=0)], 0),
        'limit': limit, 'tail': (bool, False)})
