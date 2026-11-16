"""The module exposes a documented native search contribution."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_routes_to_packaged_search_procedure():
    manifest = json.loads((ROOT / 'module/module.json').read_text())
    route = manifest['router']['routes'][0]
    assert route['procedure'] == 'session_search/search'
    assert any((ROOT / 'module/package/procedures').rglob('*search*'))
