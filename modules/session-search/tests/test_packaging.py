"""The portable package includes only the supported runtime and its migrations."""
import hashlib
import json
import runpy
import zipfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = runpy.run_path(str(ROOT / 'scripts/build-module.py'))


def test_folder_package_and_existing_output(tmp_path):
    output = BUILD['build'](tmp_path / 'release')
    manifest = json.loads((output / 'module.json').read_text())
    assert manifest['version'] == '0.1.0'
    assert manifest['core_requires'] == '>=0.1,<1'
    assert (output / 'package/session_search/fast_index.py').is_file()
    assert list((output / 'package/migrations').glob('*.sql'))
    assert not (output / 'package/session_search/server.py').exists()
    with pytest.raises(ValueError, match='already exists'):
        BUILD['build'](output)


def test_zip_has_checksum_and_module_root(tmp_path):
    archive = BUILD['build_zip'](tmp_path / 'release.zip')
    checksum = Path(str(archive) + '.sha256').read_text().split()[0]
    assert checksum == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        assert 'session_search/module.json' in bundle.namelist()
        assert 'session_search/package/session_search/fast_index.py' in bundle.namelist()
