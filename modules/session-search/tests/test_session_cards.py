"""Session summaries are Core documents; the dialogue module supplies search."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dialogue_module_has_no_card_mutations():
    source = (ROOT / 'module/package/contribution.py').read_text()
    assert "'session_search.find'" in source
    assert "'session_search.read'" in source
    assert "'session_search.save'" not in source
    assert json.loads((ROOT / 'module/module.json').read_text())['id'] == 'session_search'
