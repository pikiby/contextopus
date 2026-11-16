"""Installed folder modules write independently through their actual child SDK."""

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import hashlib
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from contextopus.core import relocation
from contextopus.startup import Application


ADAPTER = '''
from pathlib import Path
import time

def save(arguments, core):
    if "ready" not in arguments:
        return core.storage.execute(
            'INSERT INTO "{{module}}::items" VALUES (?)', [arguments["value"]])
    with core.storage.transaction(timeout_seconds=5) as transaction:
        transaction.execute('INSERT INTO "{{module}}::items" VALUES (?)',
                            [arguments["value"]])
        Path(arguments["ready"]).touch()
        deadline = time.monotonic() + 4
        while not Path(arguments["release"]).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test release signal did not arrive")
            time.sleep(.01)
    return {"committed": True}

def read(arguments, core):
    return core.storage.query('SELECT value FROM "{{module}}::items" ORDER BY value')
'''


def installable(root, module):
    folder = root / "modules" / module
    package = folder / "package"
    (package / "migrations").mkdir(parents=True)
    (package / "migrations/001-schema.sql").write_text(
        'CREATE TABLE "{{module}}::items" (value TEXT PRIMARY KEY);'
    )
    (package / "adapter.py").write_text(ADAPTER)
    manifest = {
        "id": module,
        "version": "1.0",
        "storage": {
            "engine": "sqlite", "schema_version": 1, "migrations": "migrations",
            "compatible_versions": {"min": 1, "max": 1},
        },
        "operations": [
            {"name": module + "." + name, "kind": kind,
             "handler": "adapter.py:" + name, "protocol": "contextopus-core"}
            for name, kind in (("save", "execute"), ("read", "query"))
        ],
    }
    (folder / "module.json").write_text(json.dumps(manifest))


def test_real_alpha_transaction_does_not_block_installed_beta_child(tmp_path):
    for module in ("alpha", "beta"):
        installable(tmp_path, module)
    ready, release = tmp_path / "alpha-ready", tmp_path / "release-alpha"
    with Application(tmp_path, discover=False) as app:
        for module in ("alpha", "beta"):
            assert app.execute("core.module_install", {"module": module})["state"] == "enabled"
            assert app.module_storage.for_module(module).path.is_file()
        with ThreadPoolExecutor(max_workers=2) as executor:
            alpha = executor.submit(app.execute, "alpha.save", {
                "value": "alpha committed", "ready": str(ready), "release": str(release),
            })
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and not alpha.done() and time.monotonic() < deadline:
                    time.sleep(.01)
                if alpha.done():
                    alpha.result()
                assert ready.exists(), "Alpha never acknowledged its active SQL transaction"
                beta = executor.submit(app.execute, "beta.save", {"value": "beta committed"})
                assert beta.result(timeout=2)["rowcount"] == 1
                assert app.query("beta.read")["rows"] == [{"value": "beta committed"}]
                assert not alpha.done()
                assert not release.exists()
                with app.layout.guard(deadline=time.monotonic() + .5):
                    pass
            finally:
                release.touch()
            assert alpha.result(timeout=3) == {"committed": True}
        assert app.query("alpha.read")["rows"] == [{"value": "alpha committed"}]
        assert not app.module_storage.legacy_path.exists()


