# Client installation packages

## Shared starter and per-client archives

`distribution/build.py` builds a standalone **Ready Core** starter archive (`contextopus-core-<version>.zip`) from the reviewed Core wheel, shared Python installer (`install.py`), platform wrappers (`install.sh`, `install.ps1`) and standard constraints/licenses.

`distribution/build_clients.py` wraps that same Core starter bundle into platform-specific archives for all **seven** supported assistants (`unix` for Linux/macOS and `windows` for Windows):

```console
python distribution/build.py --core-wheel dist/contextopus_core-0.1.0-py3-none-any.whl --bundle-version 0.1.0 --output /tmp/contextopus-core-0.1.0.zip
python distribution/build_clients.py /tmp/contextopus-core-0.1.0.zip /tmp/contextopus-claude-code-unix-0.1.0.zip --client claude-code --platform unix
```

Core starter and client archives ship Core alone without pre-bundling optional modules. Optional modules such as **Session Search** (`contextopus-module-session-search-0.1.0.zip`, built by `modules/session-search/scripts/build-module.py --zip`) ship as separate standalone release assets that users can add to any Core root via `contextopus modules install`.

Every bundle uses the exact same persistent data directory model — no client-specific database, separate model runtime or duplicated Core logic is introduced.

## Supported clients matrix

| Client | Package delivery | Automated verification scope |
|---|---|---|
| **Codex** | Portable plugin manifest + `.codex-plugin/plugin.json`, `.mcp.json`, `w` skill + HTTP MCP | Package structure, `test_release_readiness.py` and native adapter tests |
| **Claude Code** | Plugin layout (`plugin.json`, `.mcp.json`, `skills/w/SKILL.md`) | Plugin structure and native adapter tests |
| **Cursor** | Plugin layout (`plugin.json`, `.mcp.json`, `skills/w/SKILL.md`) | Plugin structure and native adapter tests |
| **Hermes** | Portable `plugin.json`, `mcp.json`, `skills/w/SKILL.md` | Package structure, config/profile adapter and receipt tests |
| **Antigravity** | Native `plugin.json`, `mcp_config.json`, `assets/logo.svg`, `skills/w/SKILL.md` | Package structure and native file adapter tests |
| **OpenCode** | Shared Core installer + native `opencode.json` / skills delivery | Bundle & native file adapter tests |
| **Crush** | Shared Core installer + native `crush.json` / skills delivery | Bundle & native file adapter tests |

Note: Unit tests and package validation (`scripts/verify_release.py`) verify archive structure, licenses, canonical `w` skill consistency (`plugins/contextopus/skills/w/SKILL.md` == `src/contextopus/resources/skills/w/SKILL.md`), checksums and adapter file generation. They do not guarantee compatibility with every host version or OS distribution on their own — reload the target assistant and confirm `guide()` + `core.status` after setup.

## First setup behavior

- Run `sh install.sh` (Linux/macOS) or `powershell -File install.ps1` (Windows). Add `--plan` first to inspect all planned commands without touching disk or network.
- Keep your Core root directory outside client folders and plugin caches (`--root /path/to/data`).
- Fresh setup installs Core (alongside its `zstandard` dependency required by Session Search compressed transcripts), runs `contextopus init`, optionally runs `contextopus setupmodel --device cpu --apply` (unless `--skip-model` is passed), configures client integration and binds the default root (`contextopus root --set`).
- Use `--module none` or omit `--module` for standalone Core setup; if building custom multi-module bundles with `--module /path/to/module`, `--module ID` selects included modules at install time.
- Dependencies (`uv`) and local HuggingFace model weights require network access unless `--skip-model` is used.

See [Distribution installer guide](../distribution/README.md), [Client integrations](integrations.md) and [Development](development.md).
