# Hermes native integration

Contextopus connects Hermes to the same shared loopback HTTP MCP owner (`http://127.0.0.1:8765/mcp`) used by your other clients. Core installs its unified **`w`** skill in Hermes' native skills directory, while any enabled optional modules contribute their configured routes and skills. Hermes retains ownership of its provider keys, model selection and own conversation/session state.

## Verified upstream contract

The compatibility target verified during integration development is [NousResearch/hermes-agent v0.22.2](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.2) (commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`). Review upstream tag changes before adopting newer branches.

In `v0.22.2`, Hermes reads configuration from `$HERMES_HOME/config.yaml`, skills from `$HERMES_HOME/skills/` and remote HTTP MCP servers from `mcp_servers`. Default native home locations:
- Linux / macOS: `~/.hermes`
- Windows: `%LOCALAPPDATA%\hermes` (or `~/AppData/Local/hermes` if `LOCALAPPDATA` is unset)

Upstream reference points:
- [Installation instructions](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/README.md#quick-install) and [installer script](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/scripts/install.sh)
- [Configuration](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/hermes_cli/config.py), [home resolution](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/hermes_constants.py), [profiles](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/hermes_cli/profiles.py), [MCP lifecycle](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/tools/mcp_tool_lifecycle.py) and [reload command](https://github.com/NousResearch/hermes-agent/blob/v0.22.2/hermes_cli/cli_info_mixin.py)

---

## Install and select a native Hermes home

Install Contextopus Core first using the normal [installation steps](../../README.md#install-contextopus), keeping your persistent data root and embedding setup outside Hermes folders.

Install Hermes separately from its pinned official script after inspecting and verifying its SHA-256 (`f4cdb0e20f1aca2c3871a76b962fd4db61510f549cf973759c753ac13349beb5` for `d923a5eb487a17c0a68801bba9ef0f56c81040a8`):

```console
curl --fail --location --output hermes-install-v0.22.2.sh https://raw.githubusercontent.com/NousResearch/hermes-agent/d923a5eb487a17c0a68801bba9ef0f56c81040a8/scripts/install.sh
sha256sum hermes-install-v0.22.2.sh
bash ./hermes-install-v0.22.2.sh --branch release/0.22.2 --commit d923a5eb487a17c0a68801bba9ef0f56c81040a8 --hermes-home "$HOME/.hermes" --dir "$HOME/.hermes/hermes-agent" --non-interactive --skip-browser --skip-computer-use
```

If Linux web asset bundling encounters Node memory contention (`SIGSEGV`), the `v0.22.2` script supports `HERMES_WEB_BUILD_LIGHT=1` to bound Rolldown concurrency and V8 heap size.

Once Hermes is installed, preview and apply the Contextopus Hermes adapter (which requires `--user`):

```console
contextopus --root /path/to/data integrate hermes --user
contextopus --root /path/to/data integrate hermes --user --apply
```

### Targeting profiles

Home resolution follows:
1. `--hermes-home <directory>`
2. `HERMES_HOME` environment variable
3. Native platform default (with optional literal `HERMES_DATA_DIR_SUFFIX` appended when resolving the platform default)

For a named Hermes profile, pass its explicit native home directory to `integrate`:

```console
contextopus --root /path/to/data integrate hermes --user --hermes-home /absolute/path/to/hermes/profiles/work
contextopus --root /path/to/data integrate hermes --user --hermes-home /absolute/path/to/hermes/profiles/work --apply
```

Launch Hermes with that matching profile explicitly; configuring one profile never alters another active profile.

---

## Generated configuration and files

The adapter configures `<hermes-home>/config.yaml`:

```yaml
mcp_servers:
  contextopus:
    url: http://127.0.0.1:8765/mcp
    tools:
      resources: false
      prompts: false
```

Setting `resources: false` and `prompts: false` disables Hermes' extra wrapper tools because Contextopus exposes its complete workflow through three native MCP tools: **`guide`**, **`read`** and **`write`**.

Alongside `config.yaml`, the adapter installs managed skills under `<hermes-home>/skills/` (`/w` by default, plus any enabled module skills unless `--config-only` is passed) and records an ownership receipt at `<hermes-home>/.contextopus/hermes-integration.json`.

Existing YAML comments, unrelated settings, credentials, MCP servers and user-edited skill files with colliding custom content are preserved cleanly; ambiguous or malformed YAML fails before writing.

---

## Start and verify live inside Hermes

1. Start your Contextopus Core service against the target root (`contextopus service install` / `contextopus service start`).
2. Open Hermes in the configured home/profile (or run `/reload-mcp` inside an active session).
3. Confirm all three MCP tools appear (**`guide`**, **`read`**, **`write`**) and ask Hermes to execute:
   - `guide()`
   - `read(operation="core.status")`
4. Verify that the returned `root` matches your intended Contextopus data directory.
5. Invoke `/w` on a sample read query to verify the end-to-end procedure flow.

Note: `contextopus reconnect` supports `codex`, `claude-code`, `opencode` and `crush`; for Hermes, use `/reload-mcp` or restart Hermes after restarting the Core service. Package structure checks verify the Linux/Python configuration logic only and do not imply live verification across every macOS/Windows Hermes release.

See [Client integrations](../integrations.md).
