# Core workflow (`work`)

## Loop
1. Identify intent and authorized action (`read`, `edit`, `run`, `setup`). Preserve the selected context; locate it via `core.library` or `core.search` if needed.
2. Enter context once via `core.context_read` (returns `settings`, `rules`, and `route_map`). Respect `settings`.
3. Open every `route_map` target matching the task topic via `core.library(action="record", ...)` and treat it as a mandatory task rule.
4. Execute only authorized actions within scope. Before mutating any record, call `core.documents_preview` (`action="save"` or `"delete"`), follow its `action_contract`, and only then call `core.documents_save` / `core.documents_delete`.

## Knowledge & Settings
- Search: `core.search` (inherit `settings`; verify hits by reading records). Browse/links: `core.library`, `core.documents_relations`. Absence never authorizes auto-creating notes/contexts.
- Mutations: always preview first. By default, `instructions` require explicit approval (`instructions_approval_required: true`) while notes/documents do not (`documents_approval_required: false`) unless enabled in root or context settings. When `approval_required` is true, show path and diff, wait for explicit approval of that diff, and pass `approval={"change_revision": "<from preview>", "confirmation": "<user reply>"}`.
- Partial edits: copy/edit the file directly at the path returned by MCP after approval (new `id` for copies) and call `core.documents_reconcile` if needed.
- Settings: each context can override settings independently via `core.settings` -> `core.settings_preview` -> `core.settings_apply`.

## Agents, Sessions & Host Skills
- **Profiles/Teams:** check `agents-main`, read full records in `profiles`/`teams`, verify native host support before delegating.
- **Sessions:** `session_search.list` -> `session_search.find` -> `session_search.read` (`tail=true`); authored summaries live in `collection="session_summaries"`. Run `session_search.scan` only when refresh is needed.
- **Host Skills:** cards in `skills` are metadata only; read and run the host `SKILL.md` natively.
