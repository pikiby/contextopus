"""Facade installation and incremental indexing without model inference."""
import json
from pathlib import Path
import runpy
import pytest
from contextopus.startup import Application

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def app(tmp_path):
    release = runpy.run_path(str(ROOT / 'scripts/build-module.py'))['build'](tmp_path / 'release')
    app = Application(tmp_path / 'data', discover=False)
    args = {'module': 'session_search', 'source': str(release)}
    plan = app.query('core.module_source_plan', args)
    app.execute('core.module_install', {**args, 'source_revision': plan['revision']})
    for source in ('codex', 'claude_code', 'antigravity', 'opencode', 'crush'):
        app.execute('session_search.configure', {'source': source, 'root': str(tmp_path/source)})
    yield app
    app.close()


def test_providers_compression_partial_and_truncation(app, tmp_path):
    import zstandard
    codex = tmp_path/'codex/archived_sessions/a.jsonl.zst'
    codex.parent.mkdir(parents=True)
    data = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'compressed zebra'}]}})+'\n'
    codex.write_bytes(zstandard.ZstdCompressor().compress(data.encode()))
    claude = tmp_path/'claude_code/project/c.jsonl'
    claude.parent.mkdir(parents=True)
    item = {'type': 'assistant', 'sessionId': 'c', 'message': {'content': [{'type': 'thinking', 'thinking': 'secret'}, {'type': 'text', 'text': 'visible'}]}}
    claude.write_text(json.dumps(item))
    anti = tmp_path/'antigravity/a/.system_generated/logs/transcript.jsonl'
    anti.parent.mkdir(parents=True)
    anti.write_text(json.dumps({'type': 'USER_INPUT', 'content': 'hello'})+'\n'+json.dumps({'type': 'PLANNER_RESPONSE', 'content': 'world', 'thinking': 'secret', 'tool_calls': ['heavy']})+'\n')
    assert not app.execute('session_search.scan')['errors']
    assert app.query('session_search.find', {'query': 'zebra'})['matches']
    assert app.query('session_search.read', {'session_id': 'a', 'source': 'antigravity'})['total_parts'] == 2
    assert not app.query('session_search.find', {'query': 'secret'})['matches']
    with claude.open('a') as f:
        f.write('\n')
    app.execute('session_search.scan')
    assert app.query('session_search.read', {'session_id': 'c'})['parts'][0]['text'] == 'visible'
    claude.write_text(json.dumps({'type': 'user', 'sessionId': 'c', 'message': {'content': 'replacement'}})+'\n')
    app.execute('session_search.scan')
    assert app.query('session_search.read', {'session_id': 'c'})['total_parts'] == 1
    assert not app.query('session_search.find', {'query': 'visible'})['matches']


def test_provider_web_settings(app):
    from contextopus.interfaces.web.backend import LibraryBackend
    backend = LibraryBackend(app)
    assert backend.provider_settings()['session_search']['providers']['codex']['enabled']
    backend.provider_settings(module='session_search', source='codex', enabled=False)
    assert not app.query('session_search.status')['providers']['codex']['enabled']

def test_incremental_and_disable(app, tmp_path):
    path = tmp_path/'codex/sessions/test.jsonl'
    path.parent.mkdir(parents=True)
    rows = [{'type': 'session_meta', 'payload': {'id': 'example', 'cwd': '/work'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'unique kiwi'}]}}]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    first = app.execute('session_search.scan')
    assert not first['errors']
    assert first['updated_files'] == 1
    assert app.execute('session_search.scan')['bytes_read'] == 0
    assert app.query('session_search.find', {'query': 'kiwi'})['matches'][0]['session_id'] == 'example'
    assert app.query('session_search.list', {'cwd': '/work'})['sessions'][0]['first_prompt'] == 'unique kiwi'
    with path.open('a') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'answer'}]}})+'\n')
    assert app.execute('session_search.scan')['bytes_read'] < path.stat().st_size
    assert app.query('session_search.read', {'session_id': 'example', 'tail': True, 'limit': 1})['parts'][0]['text'] == 'answer'
    app.execute('session_search.configure', {'source': 'codex', 'enabled': False})
    assert not app.query('session_search.find', {'query': 'kiwi'})['matches']
    assert not app.query('session_search.read', {'session_id': 'example'})['parts']
    app.execute('session_search.configure', {'source': 'codex', 'enabled': True})
    app.execute('core.module_disable', {'module': 'session_search'})
    app.execute('core.module_enable', {'module': 'session_search'})
    assert app.query('session_search.read', {'session_id': 'example'})['total_parts'] == 2


def test_dialogue_cleaning_and_sqlite_providers(app, tmp_path):
    import sqlite3
    codex = tmp_path / 'codex/sessions/clean.jsonl'
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(''.join(json.dumps(r) + '\n' for r in [
        {'type': 'session_meta', 'payload': {'id': 'cx1', 'cwd': '/proj'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '# AGENTS.md instructions for /proj\n<INSTRUCTIONS>ignore me</INSTRUCTIONS>\n<environment_context><cwd>/proj</cwd></environment_context>'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'real user question'}]}},
    ]))
    anti = tmp_path / 'antigravity/ag1/.system_generated/logs/transcript.jsonl'
    anti.parent.mkdir(parents=True, exist_ok=True)
    anti.write_text(json.dumps({'type': 'USER_INPUT', 'created_at': '2026-11-15T10:00:00Z', 'content': '<CONTEXT_SUMMARY>old</CONTEXT_SUMMARY><USER_REQUEST>\nclean ag ask\n</USER_REQUEST>\n<ADDITIONAL_METADATA>meta</ADDITIONAL_METADATA>'}) + '\n')

    oc_dir = tmp_path / 'opencode'
    oc_dir.mkdir(parents=True, exist_ok=True)
    oc = sqlite3.connect(oc_dir / 'opencode.db')
    oc.executescript('''
        CREATE TABLE session(id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, time_created INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        INSERT INTO session VALUES('oc1', NULL, '/oc', 1794700000000), ('oc_sub', 'oc1', '/oc', 1794700001000);
        INSERT INTO message VALUES('m1', 'oc1', 100, '{"role":"user"}'), ('m2', 'oc1', 200, '{"role":"assistant"}'), ('m3', 'oc_sub', 300, '{"role":"user"}');
        INSERT INTO part VALUES('p1', 'm1', 'oc1', 100, '{"type":"text","text":"oc question"}'),
                              ('p2', 'm2', 'oc1', 200, '{"type":"reasoning","text":"hidden"}'),
                              ('p3', 'm2', 'oc1', 201, '{"type":"text","text":"oc answer"}'),
                              ('p4', 'm3', 'oc_sub', 300, '{"type":"text","text":"subagent noise"}');
    ''')
    oc.close()

    cr_root = tmp_path / 'crush'
    cr_proj = tmp_path / 'proj/.crush'
    cr_root.mkdir(parents=True, exist_ok=True)
    cr_proj.mkdir(parents=True, exist_ok=True)
    (cr_root / 'projects.json').write_text(json.dumps({'projects': [{'path': str(tmp_path / 'proj'), 'data_dir': str(cr_proj)}]}))
    cr = sqlite3.connect(cr_proj / 'crush.db')
    cr.executescript('''
        CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, created_at INTEGER);
        CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT, created_at INTEGER, is_summary_message INTEGER);
        INSERT INTO sessions VALUES('cr1', '', 1794700000), ('cr_sub', 'cr1', 1794700001);
        INSERT INTO messages VALUES('cm1', 'cr1', 'user', '[{"type":"text","data":{"text":"crush ask"}}]', 10, 0),
                                    ('cm2', 'cr1', 'assistant', '[{"type":"reasoning","data":{"thinking":"secret"}},{"type":"text","data":{"text":"crush reply"}}]', 20, 0),
                                    ('cm3', 'cr_sub', 'user', '[{"type":"text","data":{"text":"subagent"}}]', 30, 0);
    ''')
    cr.close()

    res = app.execute('session_search.scan')
    assert not res['errors']
    assert app.query('session_search.list', {'source': 'codex'})['sessions'][0]['first_prompt'] == 'real user question'
    assert app.query('session_search.read', {'session_id': 'ag1', 'source': 'antigravity'})['parts'][0]['text'] == 'clean ag ask'
    assert [p['text'] for p in app.query('session_search.read', {'session_id': 'oc1', 'source': 'opencode'})['parts']] == ['oc question', 'oc answer']
    assert not app.query('session_search.find', {'query': 'subagent'})['matches']
    assert not app.query('session_search.find', {'query': 'hidden'})['matches']
    assert [p['text'] for p in app.query('session_search.read', {'session_id': 'cr1', 'source': 'crush'})['parts']] == ['crush ask', 'crush reply']
