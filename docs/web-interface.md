# Web interface

Start the local web interface against your running Contextopus service and open **`http://127.0.0.1:41860/`**:

```console
contextopus web
```

Pass `--root /path/to/data` or `--port 41861` before/on `web` to inspect a different root or bind another loopback port. The web server talks to your running Core owner (using its loaded index, operations and configuration) rather than opening a second database handle or model instance.

---

## Sections and capabilities

1. **Library (`Library` view):**
   - Switch between **Core Documents** (`contexts`, `instructions`, `notes`, `profiles`, `teams`, `skills`, `skill_sources`, `session_summaries`, `images`, plus any custom collections) and enabled modules that expose a library contribution (such as **Session Search**).
   - Search with **Text** mode (fast lexical filtering on titles/descriptions) or **Semantic** mode (uses Core's configured embedding index and selection algorithm, showing `considered` / `selected` diagnostics and omitted-count warnings).
   - Filter by collection, record `type` and module-specific metadata filters (for instance `source` on Session Search).
   - Read full Markdown bodies, metadata and typed outgoing/incoming links (missing targets are marked clearly as `Unavailable`). Paginated records (such as multi-turn session dialogues) load block-by-block on demand without triggering hidden background scans.

2. **Contexts and rules (`Contexts and rules` view):**
   - View root-level defaults and per-context settings for all contexts under `core/documents/contexts/`, along with resolved `workdirs` and setting origins (`root.configuration`, `context.configuration`, etc.).
   - Toggle boolean safety and provenance policies interactively (`/api/context-settings`):
     - `documents_approval_required`
     - `instructions_approval_required`
     - `git_history_enabled`

3. **Edit history (`Edit history` view):**
   - Browse chronological Git commits tracked automatically under `<root>/core/` (`documents/`, `instructions/`, `configuration/`).
   - Inspect per-file unified line diffs (`/api/history/diff`).
   - Revert an individual changed file from a commit (`/api/history/revert`), guarded strictly so later conflicting edits cannot be clobbered and deleting a document with incoming links is blocked cleanly.

4. **Session sources *(sidebar control)*:**
   - Displays provider toggles exposed by enabled modules declaring `provider_settings` (for example toggling `codex`, `claude_code`, `antigravity`, `opencode` and `crush` on Session Search via `/api/providers`). Disabled sources retain their existing index while excluding them from future scans and searches.

---

## Module library contribution

Any enabled module exposing a `query` operation named `<module_id>.library` automatically appears in the web UI without custom frontend code. Modules without one show an informative unsupported message.

Accepted arguments:
- `action`: `"collections"`, `"records"` or `"record"`
- `collection`, `query`, `mode` (`"text"` | `"semantic"`), `type`, `filters`, `offset`, `limit` (capped at 100), `id`

Returned fields:
- Collections: `{collections: [{id, title}], views?: [{title, type}], filters?: [...]}`
- Records: `{records: [{key: [module, collection, id], title, description, type, ...}], total, has_more, types?, warnings?, diagnostics?}`
- Record: `{key, title, description, type, content, metadata, links, link_groups?, path?, revision?}` (unavailable links carry `{reference, title, unavailable: true}`).

---

## Local HTTP security boundary

- Binds strictly to `127.0.0.1` and enforces `Host`, `Origin` and `Sec-Fetch-Site` same-origin checks (`403` on foreign/cross-site requests).
- Serves only packaged static assets (`/`, `/app.js`, `/style.css`) and bounded explicit endpoints (`/api/modules`, `/api/collections`, `/api/records`, `/api/record`, `/api/providers`, `/api/contexts`, `/api/context-settings`, `/api/history`, `/api/history/diff`, `/api/history/revert`) — no arbitrary MCP passthrough or unrestricted filesystem access.
- Emits strict `Content-Security-Policy` headers (`default-src 'self'`, `frame-ancestors 'none'`) and renders untrusted Markdown via DOM nodes (`textContent`) rather than executing raw HTML.
- Intended strictly for **local personal use**; do not expose directly through a reverse proxy without an external authentication layer.

## Automated checks

```console
uv run --locked pytest tests/test_web_library.py
node tests/web_frontend_smoke.cjs src/contextopus/interfaces/web/static/app.js
```

Note that automated HTTP and DOM smoke tests use simulated data fixtures and do not substitute for visual verification against a live running Core root.
