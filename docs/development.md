# Develop, build and verify Contextopus

Contextopus 0.1.0 is developed, tested and packaged from this single repository: **Ready Core** (`src/contextopus/`), client plugin assets (`plugins/contextopus/`), bundle builders (`distribution/`) and optional add-on modules (`modules/session-search/`).

## Install from source

Requires **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/):

```console
uv tool install --python 3.12 ".[local]"
contextopus --version
```

Include `".[local]"` to install `sentence-transformers` for local embeddings (or `".[tokenizers]"` when connecting remote APIs needing explicit HuggingFace/`tiktoken` tokenizers). Omit optional extras for text-only or lightweight setups.

Initialize a durable persistent data root outside source checkouts and plugin caches:

```console
contextopus --root /absolute/path/to/contextopus-data init
```

Preview and configure embeddings explicitly before enabling semantic search:

```console
contextopus --root /absolute/path/to/contextopus-data setupmodel --hardware
contextopus --root /absolute/path/to/contextopus-data setupmodel --device auto
contextopus --root /absolute/path/to/contextopus-data setupmodel --device auto --apply
contextopus root --set /absolute/path/to/contextopus-data
```

Start the local web UI (`contextopus web`) or install the user background service (`contextopus service install && contextopus service start`).

### Installing optional modules locally

Build and install **Session Search** as an optional add-on module without modifying Core:

```console
python modules/session-search/scripts/build-module.py --zip /tmp/contextopus-module-session-search-0.1.0.zip
contextopus --root /absolute/path/to/contextopus-data modules install session_search --source /tmp/contextopus-module-session-search-0.1.0.zip --plan
contextopus --root /absolute/path/to/contextopus-data modules install session_search --source /tmp/contextopus-module-session-search-0.1.0.zip
```

The output path passed to `build-module.py` must not already exist. See [Module SDK and lifecycle](modules.md) for update/disable flows.

---

## Automated tests

Sync locked dev/test dependencies and run the complete test suite plus frontend smoke test (requires **Node.js**):

```console
uv sync --locked --extra test
uv run --locked pytest -q
node tests/web_frontend_smoke.cjs src/contextopus/interfaces/web/static/app.js
```

What tests exercise:
- Deterministic fake embeddings exercise real LanceDB indexing, search selection algorithms and search-replay analytics without downloading model weights.
- Session Search tests (`modules/session-search/tests/`) exercise FTS5 scanning/reading, including `.jsonl.zst` Codex transcript decompression via `zstandard>=0.22`.
- Isolated loopback HTTP and child-process tests verify MCP, web API and module coordination.
- Configured GitHub Actions CI covers `ubuntu-latest`, `macos-latest` and `windows-latest` across Python **3.12** and **3.13**. Local passes establish behavior only on the OS/Python actually run.

---

## Build and verify release artifacts

Clean previous build artifacts and regenerate the complete release set into `dist/`:

```console
rm -rf dist
uv build --no-sources
uv run --locked python scripts/build_release.py
uv run --locked python scripts/verify_release.py
```

`build_release.py` produces all **18** release assets plus local `SHA256SUMS`:
1. `contextopus_core-0.1.0-py3-none-any.whl` and `contextopus_core-0.1.0.tar.gz`
2. Standalone Ready Core installer: `contextopus-core-0.1.0.zip`
3. Standalone optional module add-on: `contextopus-module-session-search-0.1.0.zip`
4. 14 client-tailored starter archives (`codex`, `claude-code`, `cursor`, `hermes`, `antigravity`, `opencode`, `crush` × `unix` / `windows`)

`SHA256SUMS` and `*.sha256` sidecars stay local for artifact validation and are excluded from GitHub Release uploads.

`verify_release.py` verifies `SHA256SUMS`, Core-only bundle identity (confirming no optional modules leak into Core), wheel CLI entrypoints and licenses, source parity inside `.tar.gz`, canonical `w` skill parity across `src/` and `plugins/`, all 14 client ZIPs and `inspect_source()` validation of `contextopus-module-session-search-0.1.0.zip`.

---

## Verify an installed wheel in isolation

Test the exact built wheel inside a disposable virtual environment before updating a live system:

```console
/path/to/disposable/bin/python -I scripts/verify_installed.py --expected-version 0.1.0 --prefix /path/to/disposable
```

(On Windows, use `\path\to\disposable\Scripts\python.exe`.) This verifier validates CLI/entrypoints, bare-Core MCP dispatch and folder module installation/storage/restart in a temporary directory without installing OS services or touching your live user data.

---

## Upgrading existing roots

Always back up your persistent root coherently before applying an upgrade (preserving `core/`, `modules/` and configuration). Test configuration or module updates carefully and preview native client integrations before applying.

See [Release 0.1.0 notes](releases/0.1.0.md).
