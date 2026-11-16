# Root-local modules

Contextopus modules extend a Core root with optional operations, procedures, skills, filter algorithms, provider controls and Web UI views — without adding those dependencies or storage schemas into the base Core package.

Built-in add-ons such as [Session Search](../modules/session-search/README.md) are distributed as standalone `.zip` archives (`contextopus-module-session-search-<version>.zip`) right on the Contextopus release page, or built locally from source. Custom modules follow the exact same package contract.

## Folder layout and manifest contract

A built module package contains `module.json` and `package/`. Mutable runtime user data and module configuration belong under `<root>/modules/<id>/data/` and `<root>/modules/<id>/config/`, separate from replaceable code under `<root>/modules/<id>/`. Discovery reads `module.json` passively without importing code, installing dependencies or running migrations. Desired enablement and prepared fingerprints persist under `<root>/core/configuration/modules.json`.

Example `module.json`:

```json
{
  "id": "sample",
  "version": "0.1.0",
  "core_requires": ">=0.1,<1",
  "dependencies": {"python": []},
  "storage": {
    "engine": "sqlite",
    "schema_version": 1,
    "migrations": "migrations",
    "compatible_versions": {"min": 1, "max": 1}
  },
  "operations": [{
    "name": "sample.status",
    "kind": "query",
    "handler": "adapter.py:status",
    "arguments_schema": {"type": "object", "additionalProperties": false}
  }]
}
```

Contract basics:
- Module `id` uses lowercase letters, digits, underscores and hyphens, starting with a letter (`"core"` is reserved).
- Versions and `core_requires` use PEP 440 specifications (for Core 0.1.x, declare `">=0.1,<1"`).
- Every contributed operation name must start with `<module_id>.`.
- Optional manifest fields inside `package/` include `procedures`, `skills`, `filter_algorithms`, `provider_settings`, `router`, `contribution` or `service` (paths cannot escape `package/`).
- Optional `provider_settings` exposes `{"status": "<id>.status", "configure": "<id>.configure"}` so Web UI **Session sources** can toggle providers automatically.
- An optional query operation named `<module_id>.library` automatically exposes collections, records and reading views inside `contextopus web` (see [web library contribution](web-interface.md#module-library-contribution)).

---

## Router contribution (`guide()`)

`guide()` without arguments acts as Core's single router, combining Core base rules (`resources/base_rules.md`), Core routes (`work`, `modules`, `embeddings`, `setup`) and all enabled module routes and steps:

```json
"router": {
  "routes": [{"id": "search", "when": "Search transcript history", "procedure": "sample/search", "order": 100}],
  "steps": [{"id": "check", "text": "Verify module status first.", "procedure": "sample/search",
             "required": true, "in": ["guide"], "order": 100}]
}
```

- Calling `guide(procedure="sample")` lists the enabled module's procedure names and headings.
- Calling `guide(procedure="sample/search")` returns that Markdown procedure alongside base rules, enabled guide steps and live schemas of any operations declared in its YAML front matter (`operations: [...]`).
- Modules cannot override Core's default router. Users can customize route/step `enabled`, `order`, `when`, `required`, `in` (`["guide"]`) or `text` via Core root settings (`router`).

---

## Install, inspect, enable, disable and update

All interfaces (CLI, MCP and `modules` procedure) share the same lifecycle operations on `<root>/modules/<id>`:

### From a built ZIP or directory *(recommended)*

Core accepts a trusted built folder or ZIP whose root (or single `<id>/` folder) contains `module.json` and `package/`:

```console
contextopus --root /path/to/data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip --plan
contextopus --root /path/to/data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip
```

`--plan` validates the payload structure, manifest, safe archive paths (no symlinks, encrypted archives, path traversal, case-colliding entries; bounds: 10,000 entries, 256 MiB file limit, 512 MiB total uncompressed limit) and computes a fingerprint without staging code. Raw source checkouts containing `.git`, virtualenvs or mutable state must be built first.

For an already-placed folder at `<root>/modules/<id>/`, `contextopus --root /path/to/data modules install <id>` prepares dependencies/storage and enables it. Repeat the same command after resolving an interrupted install.

### MCP operations

| Operation | Kind | Purpose |
|---|---|---|
| `core.modules` | `query` | List discovered modules, states (`storage_pending`, `enabled`, `disabled`, `error`), compatibility and operations. |
| `core.module_source_plan` | `query` | Inspect and fingerprint a built folder or ZIP (`module`, `source`) before staging. |
| `core.module_install` | `execute` | Install from staged folder (`module`) or reviewed source (`module`, `source`, `source_revision`). |
| `core.module_prepare` | `execute` | Validate compatibility and prepare Python dependencies without migrating storage or importing handlers. |
| `core.module_enable` | `execute` | Atomically register operations, procedures, skills and algorithms once prepared and storage-compatible. |
| `core.module_disable` | `execute` | Withdraw active operations, procedures, skills and algorithms while keeping `data/`, `config/` and SQLite files intact. |
| `core.module_update` | `execute` | Stage replacement code under `.releases/`, update `.current.json` atomically, migrate storage cleanly and fall back safely if validation or migration fails. |

### Native client skills delivery

If a module includes `skills` under its package (`package/skills/<name>/SKILL.md`), enabling the module makes those skills available to `contextopus integrate <client> --user --apply`. Refresh client integration whenever enabling or disabling modules that contribute native skills; unchanged managed files update cleanly while user-edited files are protected.

---

## Execution modes

Modules run in one of two integration models depending on their needs:

### 1. Trusted in-process contributions (`contribution`)

Declare `"contribution": {"factory": "contribution.py:contribute", "requires": ["..."]}` inside `module.json`.

- Core loads the factory inside Core's Python interpreter once verified and enabled, granting direct access to the shared SQL/RAG/settings/procedure APIs.
- Declared `requires` packages must already exist in Core's Python environment (for instance, `zstandard>=0.22` for Session Search is shipped in `contextopus-core` dependencies).
- Installation executes paired `install_prepare` / `install_apply` hooks safely under Core locks; disable and update drain admitted calls cleanly before teardown.

### 2. Child process / persistent service (`handler`, `command`, `service`)

- **Stateless child handlers:** Python `"handler": "mod.py:fn"` (runs in isolated module `.venv` created via `uv` if dependencies exist) or executable `"command": ["bin", "arg"]`. Using `"protocol": "contextopus-core-v1"` communicates over newline-delimited JSON with access to `core.storage.query` / `core.storage.execute`.
- **Persistent worker service (`service`):** Declare `"service": {"factory": "worker.py:create_service", "ready_timeout": 10, "stop_timeout": 5}` and `"service": true` on operations. Core's root HTTP owner starts enabled services, routes operations via `dispatch(operation, arguments, context)`, exposes optional `core.embeddings` / `core.rag` (`"rag": true`) and stops workers cleanly on disable, update or shutdown. Frame size is bounded to 1 MiB UTF-8 JSON per message.

---

## Authoring structured storage (SQLite)

Each module declaring `storage` receives an isolated SQLite database at `<root>/core/storage/modules/<module-id>.sqlite3` with per-module write serialization:

- Supply continuous migration SQL files in `migrations/` (`001-initial.sql`, `002-...sql`) up to `schema_version`.
- Core replaces `{{module}}` with the validated module ID (for example `CREATE TABLE "{{module}}::sessions" (...)`).
- Core rejects cross-module table references, `ATTACH`, direct `PRAGMA`, extension loading and explicit DDL/transaction statements outside migrations.
- With the Core storage bridge (`protocol: "contextopus-core-v1"` or child SDK), use `core.storage.query(sql, parameters)` (returns `columns`, `rows`), `core.storage.execute(sql, parameters)` (returns `rowcount`, `lastrowid`) or `with core.storage.transaction(timeout_seconds=5) as tx:`. Keep transactions short and outside file I/O, tokenization or embedding calls.
- SQLite commits and LanceDB vector index updates remain independent — retain durable work/deletion queues inside SQLite whenever combining structured storage with `"rag": true`.

---

## Procedure front matter and input errors

Procedures in a module's `procedures/` Markdown folder should declare directly used operations in YAML front matter:

```yaml
---
operations:
  - sample.status
  - sample.scan
---
```

Core attaches live JSON schemas and MCP tool hints (`read` vs `write`) from the active registry whenever `guide(procedure="<module>/<name>")` returns that procedure. If an unlisted registered operation is needed later, call `guide(operation="<name>")`.

Registry validation failures return structured errors (`unknown_operation`, `wrong_tool`, `invalid_arguments`) preserving `isError=true` and including `execution_started` (`false` when rejected prior to invoking the handler) so callers know whether state may have been touched.
