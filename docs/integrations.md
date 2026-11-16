# Connect clients to Contextopus

One shared Core service runs at `http://127.0.0.1:8765/mcp`. Data, search indexes and model configuration remain under your persistent Core data root — outside client configuration directories and plugin caches. Installing or refreshing a client integration never creates a separate database.

## Client installation bundles

See [Client packages](client-packages.md) for package formats and the verification matrix. Fresh bundle setup detects an existing Core root or installs the shared Python runtime, connects the requested client and configures the endpoint. Do not install a duplicate runtime when a service already runs on the target root; inspect `contextopus service status` and repair or upgrade explicitly instead.

## Native file delivery

Supported adapters: `codex`, `claude-code`, `cursor`, `opencode`, `crush`, `hermes`, `antigravity`.

Preview changes first without writing, then confirm with `--apply`:

```console
contextopus --root /path/to/data integrate claude-code --user
contextopus --root /path/to/data integrate claude-code --user --apply
```

Scope options:
- `--user`: configure user-level native MCP and skill files. Required for `hermes` and `antigravity`.
- `--project DIRECTORY`: configure project-level MCP and skill files (supported for `codex`, `claude-code`, `cursor`, `opencode`, `crush`).
- `--hermes-home DIRECTORY`: target a specific native Hermes home or profile when using `integrate hermes --user`; see the [Hermes guide](integrations/hermes.md).

## Skill delivery and aliases

Core ships a unified canonical routing skill named **`w`**. Enabled modules that package their own `skills/<name>/SKILL.md` files also participate automatically when an explicit `--root` is bound or supplied.

Customize delivered skills or command aliases without altering source packages:

```console
contextopus --root /path/to/data integrate codex --user --skill w --alias w=ctx
contextopus --root /path/to/data integrate codex --user --skill w --alias w=ctx --apply
```

Flags controlling skill delivery:
- `--skill NAME` (repeatable): restrict delivery to selected canonical skill names; omitting `--skill` reuses saved choices.
- `--all-skills`: reset selection to all available enabled entrypoints.
- `--alias CANONICAL=INSTALLED` (repeatable): assign custom invocation names; replaces saved aliases when supplied.
- `--reset-aliases`: restore canonical names.
- `--config-only`: write or update MCP config only without copying skills.
- `--native-plugin`: perform user cutover for Codex or Antigravity after installing via native plugin, preserving older plugin caches.
- `--legacy-skills DIRECTORY`: migrate an explicit older manual skill directory (never a plugin cache).

Integration previews report affected paths without exposing unrelated secrets. Known file or name collisions stop before any write occurs, and comment-bearing `.jsonc` configuration files (such as OpenCode `opencode.jsonc`) are rejected rather than stripped. Receipts track installed file hashes so updates upgrade unchanged managed skills while preserving files modified by the user.

## Plugin delivery vs file delivery

- For plugin-capable assistants (`Codex`, `Claude Code`, `Cursor`, `Hermes`, `Antigravity`), release bundles can install a native plugin package (`plugins/contextopus/` exposes `w` under the `contextopus` plugin namespace). Avoid copying the exact same skill paths into both native file folders and an active plugin for the same workspace unless switching via `--native-plugin`.
- `OpenCode` and `Crush` receive native MCP config + skill files cleanly without plugin packaging.
- Plugin installation alone does not start Core or silently download embedding weights: start `contextopus service start` first so `http://127.0.0.1:8765/mcp` is reachable.

## Refresh and verify live connections

After restarting `contextopus service` or adding tools, reconnect supported clients (`codex`, `claude-code`, `opencode`, `crush`):

```console
contextopus reconnect codex
contextopus reconnect opencode --client-url http://127.0.0.1:4096 --directory /path/to/project
```

Always reload MCP (or restart the client) and run a real tool check inside the assistant — ask it to invoke **`guide()`** and **`read(operation="core.status")`** — confirming the response returns your expected Core data root. File generation or passing unit tests alone does not verify a live client session.
