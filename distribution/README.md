# Install Contextopus

Contextopus distributes a **standalone Core bundle** (`contextopus-core-<version>.zip`) plus client-tailored archives for **Codex, Claude Code, Cursor, Antigravity, Hermes, OpenCode and Crush** on **Unix** (Linux/macOS) and **Windows**.

Optional add-on modules such as **Session Search** (`contextopus-module-session-search-<version>.zip`) are distributed separately from Core and can be installed into your Core data root at any time.

## Quick setup

1. Download the archive for your coding assistant and OS (`contextopus-<client>-<unix|windows>-<version>.zip`) or `contextopus-core-<version>.zip` from the release page, then extract it.
2. Preview or run the installer:
   - Linux/macOS: `sh install.sh`
   - Windows: `powershell -File install.ps1`
3. Choose or review your persistent data root (keep it outside plugin caches and git checkouts), embedding model setup and client integration, then confirm.
4. Reload or restart your coding assistant and verify that `guide()` and `read(operation="core.status")` return your selected data root.

## Installer options

- `--plan`: validates the bundle and prints planned commands without creating directories, accessing the network or making changes (requires Python 3.12+).
- `--root PATH`: sets the new/empty persistent data directory for a fresh installation.
- `--client NAME`: selects native user integration (`none`, `codex`, `claude-code`, `cursor`, `opencode`, `crush`, `hermes`, `antigravity`; repeatable). Client-specific archives preselect their own client; plugin bundles avoid duplicating native skill files.
- `--module ID` / `--module none`: selects bundled module IDs when present in a custom bundle; Core release bundles contain Core alone (`none`).
- `--skip-model`: installs the local runtime and initializes the root without downloading embedding weights yet (semantic search remains unavailable until `contextopus setupmodel --apply` is run).
- `--yes`: runs non-interactively with the specified options.

## Adding optional modules after Core setup

Download a standalone module archive such as `contextopus-module-session-search-<version>.zip` and install it into your Core root:

```console
contextopus --root /path/to/data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip --plan
contextopus --root /path/to/data modules install session_search --source /path/to/contextopus-module-session-search-0.1.0.zip
```

## Notes and boundaries

- Dependencies and local model weights (`sentence-transformers`) require network access during fresh setup unless `--skip-model` is passed; these bundles are not offline installers.
- The installer handles a fresh root (`new or empty`). Existing roots should be backed up and updated through Core's explicit CLI/update workflow rather than overwritten.
- Installing Contextopus does not install your coding assistant itself.

See the [project README](../README.md), [client packages](../docs/client-packages.md), [client integrations](../docs/integrations.md) and [development & build guide](../docs/development.md).
