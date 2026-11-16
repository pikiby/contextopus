"""The packaged module uses the native facade without a daemon adapter."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_package_does_not_depend_on_external_daemon(tmp_path):
    release = runpy.run_path(str(ROOT / 'scripts/build-module.py'))['build'](tmp_path / 'release')
    manifest = (release / 'module.json').read_text()
    assert 'contribution.py:contribute' in manifest
    assert 'socket' not in manifest
    assert not (release / 'package/session_search/server.py').exists()
