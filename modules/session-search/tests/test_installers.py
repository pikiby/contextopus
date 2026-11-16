"""Module manifests declare the requirements used by the Core installer."""
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_module_installer_contract_is_packaged(tmp_path):
    release = runpy.run_path(str(ROOT / 'scripts/build-module.py'))['build'](tmp_path / 'module')
    manifest = json.loads((release / 'module.json').read_text())
    assert manifest['id'] == 'session_search'
    assert manifest['contribution']['factory'] == 'contribution.py:contribute'
    assert 'zstandard>=0.22' in manifest['contribution']['requires']
    assert (release / 'package/contribution.py').is_file()
    assert (release / 'package/procedures').is_dir()
