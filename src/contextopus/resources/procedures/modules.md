---
operations: ["core.module_disable", "core.module_enable", "core.module_install", "core.module_source_plan", "core.module_update", "core.modules"]
---

# Manage Contextopus modules

Use this procedure for modules on the connected Core server (server owns `<root>/modules`). Discovery via `core.modules` is read-only and never authorizes install/update/disable. Fetch unfamiliar schemas with `guide(operation=...)`.

## Install & Update
- **From built folder/ZIP:** `core.module_source_plan(module=<id>, source=<path>)` -> inspect plan -> `core.module_install(module=<id>, source=<path>, source_revision=<revision>)`. Packages containing source checkouts, symlinks, live state, or caches must be built first.
- **Already staged under `<root>/modules/<id>`:** `core.module_install(module=<id>)` (also retries failed installs while retaining data).
- **Update existing module:** `core.module_update(module=<id>, source=<built path>)`. Preserves user files and enabled state.
- **Enable / Disable:** only when requested (`core.module_enable` / `core.module_disable`). Disabling preserves stored data.

## Storage & Verification
- Modules may use isolated Core-managed SQLite storage (`migrations/`), background services, or shared Core RAG sources.
- After install/update, inspect `core.modules` and verify one live module operation within authorized scope; report any failed check or schema error directly.
