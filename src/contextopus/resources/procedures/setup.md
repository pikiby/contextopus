---
operations: ["core.doctor", "core.init", "core.model_apply", "core.model_plan", "core.settings"]
---

# Runtime setup and recovery

Use this procedure for actual tool, service, or embedding failures. Reuse existing authority; report observed failures and ask only for missing permissions.

## Service & Client Check
- Verify MCP tools exposed (`guide`, `read`, `write`; legacy tools only if `CONTEXTOPUS_LEGACY_TOOLS=1`) and test `guide(operation="...")`.
- CLI diagnostics: `contextopus --version`, `contextopus service status`, `contextopus doctor`. Requires Python 3.12+; installs belong in uv with explicit optional `local` extra when authorized.
- Single persistent MCP HTTP owner at `http://127.0.0.1:8765/mcp` managed via `contextopus service install|start|stop|restart|status`. Avoid duplicate servers on another root.
- Client reconnect after service restart: Codex (`contextopus reconnect codex [--thread ID]`), OpenCode (`reconnect opencode --client-url URL --directory PROJECT`), Claude Code (`/mcp` reconnect), or resume session if host cannot reload live.

## Local Model & Initialization
- Active profile lives in `core.settings` (or fallback `<root>/core/rag/active.json`). Inspect local cache (`SENTENCE_TRANSFORMERS_HOME`, `HF_HUB_CACHE`, `HF_HOME`, default `~/.cache/huggingface/hub`) without loading weights first; disclose uncached weight download before loading.
- Root initialization (`core.init` / `contextopus init`) initializes storage, bundles base documentation, and runs module setup hooks. If `model_setup_required`, plan with `core.model_plan` and apply via `core.model_apply`.
