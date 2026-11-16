# Contextopus Session Search

**Find past agent sessions by keywords, paths, clients and time — without altering original transcript files.**

Session Search is an **optional add-on module** for Contextopus Core (`session_search`, requiring Core `>=0.1,<1`). It indexes local session dialogue across **Codex** (`.jsonl` and zstd-compressed `.jsonl.zst`), **Claude Code**, **Antigravity**, **OpenCode** and **Crush** using **SQLite FTS5** lexical search. Original transcript files stay strictly read-only.

## Download and install

### Option A: From a Contextopus Release archive *(recommended)*

Download **`contextopus-module-session-search-0.1.0.zip`** from the [Contextopus Releases](https://github.com/pikiby/contextopus/releases) page and install it into your Core data root:

```console
contextopus --root /path/to/contextopus-data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip --plan
contextopus --root /path/to/contextopus-data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip
```

### Option B: Build from this repository

Build a clean folder or ZIP artifact first (the output path must not already exist):

```console
python modules/session-search/scripts/build-module.py --zip /tmp/contextopus-module-session-search.zip
contextopus --root /path/to/contextopus-data modules install session_search --source /tmp/contextopus-module-session-search.zip --plan
contextopus --root /path/to/contextopus-data modules install session_search --source /tmp/contextopus-module-session-search.zip
```

For an already installed `session_search` module, use `contextopus --root /path/to/data modules update session_search --source <path-to-built-zip-or-dir>` to preserve existing configuration and indexed data.

> Note: Reading compressed Codex transcripts requires `zstandard>=0.22` in Core's Python environment (included as a standard dependency of `contextopus-core` 0.1.0).

## Usage in MCP, CLI and Web UI

When enabled, Session Search registers a `search` route in Core's `guide()` router (pointing to procedure `session_search/search`) and exposes six operations:

| Operation | Kind | Purpose |
|---|---|---|
| `session_search.status` | `query` | Report provider roots, enabled flags, indexed session counts and pending files. |
| `session_search.configure` | `execute` | Enable/disable a provider or adjust its root directory (`codex`, `claude_code`, `antigravity`, `opencode`, `crush`). |
| `session_search.scan` | `execute` | Scan transcript files incrementally (`source?`, `max_bytes?`); repeat if status reports remaining pending files. |
| `session_search.list` | `query` | List recently modified sessions with `source`, `cwd`, `since` and first/last user prompts. |
| `session_search.find` | `query` | Run SQLite FTS5 lexical queries (`query`, `source?`, `limit?`); phrase quoting follows FTS5 syntax. |
| `session_search.read` | `query` | Read clean user/assistant dialogue (`session_id`, `source?`, `tail=true` or `start_part` offset, `limit`). |

In a coding assistant, invoke `/w` (or ask the agent to run Contextopus `guide(procedure="session_search/search")`) to look up past work.

### Web UI integration

Session Search implements `session_search.library` and `provider_settings`, so it automatically appears in the local Web Manager (`contextopus web`):
- Toggle providers (`codex`, `claude_code`, `antigravity`, `opencode`, `crush`) under **Session sources** in the sidebar.
- Browse and lexically search indexed sessions in the **Library** view and read paginated conversation turns.

Note: Opening Session Search in the Web UI or running `find`/`list` queries reads the latest indexed state and **does not trigger automatic transcript scans or background watchers**. Run `session_search.scan` explicitly when you want new sessions indexed.

## Behavior and boundaries

- **Lexical FTS5, not semantic embeddings:** Queries match tokens and quoted FTS5 phrases rapidly inside Core-managed SQLite storage (`core/storage/modules/session_search.sqlite3`) without calling an embedding model.
- **Pair with `session_summaries` for durable notes:** Agents can synthesize important findings from `session_search.read` into persistent Markdown summaries in Core's built-in `session_summaries` collection for semantic search via `core.search`.
- **Incremental & read-only:** Unchanged transcript files are skipped on rescan. An incomplete final JSONL line remains pending until completed. Disabling a source excludes it from scan/list/find/read while keeping previously indexed rows. Missing results do not guarantee a phrase is absent if files remain unscanned or failed to parse.
- **No session mutation:** Session Search never edits, archives, resumes or deletes original client transcripts.

See the [Contextopus README](../../README.md) for overall setup and [module documentation](../../docs/modules.md) for module lifecycle details.
