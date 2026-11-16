"""Regression coverage for context resolution, catalog, mutations and history."""
import http.client
import json
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from contextopus.core.documents.collections import DocumentCollections
from contextopus.core.documents.contexts import ContextReader
from contextopus.core.documents.mutations import DocumentMutations
from contextopus.core.history import GitRepository
from contextopus.core.storage import StorageLayout
from contextopus.interfaces.web.server import LibraryServer
from contextopus.startup import Application


def test_context_workdirs_longest_match(tmp_path):
    c = DocumentCollections(StorageLayout.resolve(tmp_path))
    c.save('contexts', 'outer/Context.md', {'id': 'a', 'type': 'context', 'workdirs': ['/work/project']}, '')
    c.save('contexts', 'inner/Context.md', {'id': 'b', 'type': 'context', 'workdir': '/work/project/nested'}, '')
    reader = ContextReader(c, None)
    assert reader.read('outer')['context']['id'] == 'a'
    result = reader.read(cwd='/work/project/nested/deeper')
    assert result['context']['id'] == 'b'
    assert result['resolved_workdir'] == '/work/project/nested'
    assert reader.read('/work/project/file')['context']['id'] == 'a'
    with pytest.raises(FileNotFoundError):
        reader.read(cwd='/work/project-other')


def test_registered_context_read_accepts_cwd_path_and_slug(tmp_path):
    app = Application(tmp_path, discover=False)
    app.collections.index = None
    app.collections.save('contexts', 'project/Context.md', {
        'id': 'context-id', 'type': 'context', 'slug': 'project',
        'workdirs': ['/work/project'],
    }, '')
    try:
        for args in ({'cwd': '/work/project/sub'}, {'id': '/work/project/sub'}, {'id': 'project'}):
            result = app.query('core.context_read', args)
            assert result['context']['id'] == 'context-id'
            if args.get('cwd') or args['id'].startswith('/'):
                assert result['resolved_workdir'] == '/work/project'
    finally:
        app.close()


def test_context_search_scopes_and_fallback(tmp_path, monkeypatch):
    app = Application(tmp_path, context='context-id', discover=False)
    app.collections.index = None
    app.collections.save('contexts', 'project/Context.md', {'id': 'context-id', 'type': 'context'}, '')
    calls = []
    monkeypatch.setattr(app, '_refresh_algorithms', lambda: None)
    monkeypatch.setattr(app.index, 'search_detailed', lambda query, **kw: calls.append(kw) or {'diagnostics': {'effective_settings': {'parameters': {}}}})
    app.search('test')
    assert calls[-1]['scopes'][0] == {'module': 'core', 'collection': 'notes', 'prefix': 'project/'}
    assert {s['collection'] for s in calls[-1]['scopes']} >= {'notes', 'skills', 'profiles', 'teams', 'session_summaries'}
    card = app.collections.read('contexts', 'project')
    app.collections.save('contexts', card['path'], {**card['metadata'], 'search_scope': [{'module': 'core', 'collection': 'custom'}]}, '', expected_revision=card['revision'])
    app.search('test')
    assert calls[-1]['scopes'][0] == {'module': 'core', 'collection': 'custom'}
    app.close()


def test_skill_sources_reconcile_preserves_links(tmp_path):
    c = DocumentCollections(StorageLayout.resolve(tmp_path))
    source = tmp_path / 'source'
    (source / 'one').mkdir(parents=True)
    (source / 'one' / 'SKILL.md').write_text('---\nname: one\ndescription: First skill\n---\n', encoding='utf-8')
    c.save('skill_sources', 'one.md', {'id': 'source', 'type': 'skill_source', 'root': str(source)}, '')
    c.save('skill_sources', 'missing.md', {'id': 'missing', 'type': 'skill_source', 'root': str(tmp_path / 'absent')}, '')
    c.reconcile()
    card = c.list('skills')[0]
    assert card['metadata']['source_id'] == 'source'
    assert card['metadata']['description'] == 'First skill'
    c.save('skills', card['path'], {**card['metadata'], 'references': ['[[manual]]']}, card['content'], expected_revision=card['revision'])
    (source / 'one' / 'SKILL.md').write_text('---\nname: one\ndescription: Updated skill\n---\n', encoding='utf-8')
    c.reconcile()
    card = c.list('skills')[0]
    assert card['metadata']['description'] == 'Updated skill'
    assert card['metadata']['references'] == ['[[manual]]']


def test_duplicate_guard_and_override(tmp_path):
    c = DocumentCollections(StorageLayout.resolve(tmp_path))
    existing = c.save('notes', 'project/old.md', {'id': 'old', 'type': 'note', 'title': 'Same'}, '')
    class Index:
        def search_detailed(self, query, **kwargs):
            assert kwargs['scopes'][0]['prefix'] == 'project/'
            return {'results': [{'key': ['core', 'notes', 'old'], 'path': 'project/old.md', 'metadata': {'title': 'Same'}, 'distance': .1, 'revision': existing['revision']}]}
        def index(self, doc): pass
    c.index = Index()
    settings = SimpleNamespace(snapshot=lambda **kw: {'values': {'documents_approval_required': False}, 'sources': {}})
    mutations = DocumentMutations(c, settings)
    metadata = {'id': 'new', 'type': 'note', 'title': 'Same'}
    assert mutations.preview('notes', 'project/new.md', metadata=metadata, content='')['similar']
    assert mutations.save('notes', 'project/new.md', metadata, '')['status'] == 'similar_found'
    assert not (c.collection_root('notes') / 'project/new.md').exists()
    result = mutations.save('notes', 'project/new.md', {**metadata, 'allow_similar': True}, '')
    assert result['saved'] and result['similar']
    assert 'allow_similar' not in c.read('notes', 'new')['metadata']


def test_git_history_and_web_endpoints(tmp_path):
    app = Application(tmp_path, discover=False)
    app.collections.index = None
    app.collections.save('contexts', 'project/Context.md', {'id': 'ctx', 'type': 'context', 'workdir': '/work/project'}, '')
    repo = GitRepository(tmp_path / 'core')
    repo.ensure()
    note = app.collections.save('notes', 'project/test.md', {'id': 'note', 'type': 'note', 'title': 'Before'}, 'before')
    commit = repo.log()[0]['commit']
    note = app.collections.save('notes', note['path'], {**note['metadata'], 'title': 'After'}, 'after', expected_revision=note['revision'])
    commit = repo.log()[0]['commit']
    assert 'after' in repo.diff(commit, 'documents/notes/project/test.md')
    assert b'before' in repo.file_at_parent(commit, 'documents/notes/project/test.md')
    try:
        server = LibraryServer(app, port=0)
    except PermissionError:
        app.close()
        pytest.skip('Loopback sockets are unavailable in this sandbox')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None, origin=True):
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        headers = {'Origin': f'http://127.0.0.1:{server.server_port}'} if origin else {}
        data = None if body is None else json.dumps(body)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        client.request('GET' if body is None else 'POST', path, body=data, headers=headers)
        response = client.getresponse()
        result = response.status, json.loads(response.read())
        client.close()
        return result
    try:
        status, overview = request('/api/contexts')
        assert status == 200 and overview['contexts'][0]['workdirs'] == ['/work/project']
        assert request('/api/context-settings', {'context': 'ctx', 'field': 'git_history_enabled', 'value': False})[0] == 200
        assert request('/api/context-settings', {'context': 'ctx', 'field': 'git_history_enabled', 'value': True})[0] == 200
        assert request('/api/history')[0] == 200
        assert request('/api/history/diff?' + urlencode({'commit': commit, 'path': 'documents/notes/project/test.md'}))[0] == 200
        status, _ = request('/api/history/revert', {'commit': commit, 'path': 'documents/notes/project/test.md'})
        assert status == 200
        assert app.collections.read('notes', 'note')['content'] == 'before'
        assert request('/api/context-settings', {'context': 'ctx', 'field': 'git_history_enabled', 'value': False}, origin=False)[0] == 403
        assert request('/api/context-settings', {'context': 'ctx', 'field': 'unknown', 'value': False})[0] == 400
        assert request('/api/history/revert', {'commit': commit, 'path': '../outside'})[0] == 400
        assert request('/api/history/diff?' + urlencode({'commit': commit, 'path': '../outside'}))[0] == 400
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        client.request('GET', '/api/history', headers={'Origin': 'https://example.invalid'})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        app.close()


def test_similar_location_normalized_title_and_other_context(tmp_path):
    c = DocumentCollections(StorageLayout.resolve(tmp_path))
    old = c.save('notes', 'project/old.md', {'id': 'old', 'type': 'note', 'title': 'Same Title'}, '')
    class Index:
        def search_detailed(self, query, **kwargs):
            return {'results': [
                {'key': ['core', 'notes', 'other'], 'location': 'other/note.md', 'metadata': {'title': 'Same Title'}, 'distance': .01},
                {'key': ['core', 'notes', 'old'], 'location': 'project/old.md', 'metadata': {'title': 'Same  Title'}, 'distance': .75, 'revision': old['revision']},
            ]}
        def index(self, doc): pass
    c.index = Index()
    settings = SimpleNamespace(snapshot=lambda **kw: {'values': {'documents_approval_required': False}, 'sources': {}})
    mutations = DocumentMutations(c, settings)
    metadata = {'id': 'new', 'type': 'note', 'title': ' same title '}
    preview = mutations.preview('notes', 'project/new.md', metadata, '')
    assert [(x['id'], x['path']) for x in preview['similar']] == [('old', 'project/old.md')]
    result = mutations.save('notes', 'project/new.md', metadata, '')
    assert result['status'] == 'similar_found'
    # A close hit in a different context cannot block a new note.
    c.index.search_detailed = lambda *args, **kw: {'results': [{'key': ['core', 'notes', 'other'], 'location': 'other/note.md', 'metadata': {'title': 'Same Title'}, 'distance': .01}]}
    assert mutations.save('notes', 'project/new.md', metadata, '')['saved']


def test_similar_errors_and_existing_update_need_no_preview(tmp_path):
    c = DocumentCollections(StorageLayout.resolve(tmp_path))
    class BrokenIndex:
        def search_detailed(self, *args, **kwargs): raise RuntimeError('embedding unavailable')
        def index(self, doc): pass
    c.index = BrokenIndex()
    settings = SimpleNamespace(snapshot=lambda **kw: {'values': {'documents_approval_required': False}, 'sources': {}})
    mutations = DocumentMutations(c, settings)
    metadata = {'id': 'note', 'type': 'note', 'title': 'New'}
    created = mutations.save('notes', 'project/note.md', metadata, 'first')
    assert created['saved'] and created['similar'] == []
    # Updates must not call similarity search even if a similar hit exists.
    def unexpected(*args, **kwargs): raise AssertionError('similar search called for update')
    mutations._find_similar = unexpected
    updated = mutations.save('notes', 'project/note.md', metadata, 'updated', expected_revision=created['revision'])
    assert updated['saved'] and c.read('notes', 'note')['content'] == 'updated'


def test_history_global_and_context_disable_suppresses_commits(tmp_path):
    from contextopus.interfaces.web.backend import LibraryBackend
    app = Application(tmp_path, discover=False)
    app.collections.index = None
    backend = LibraryBackend(app)
    app.collections.save('contexts', 'project/Context.md', {'id': 'context-uuid', 'type': 'context'}, '')
    repo = GitRepository(tmp_path / 'core')
    backend.update_context_setting('context-uuid', 'git_history_enabled', False)
    before = repo.log()[0]['commit']
    app.collections.save('notes', 'project/no-history.md', {'id': 'no-history', 'type': 'note'}, '')
    assert repo.log()[0]['commit'] == before
    backend.update_context_setting(None, 'git_history_enabled', False)
    before = repo.log()[0]['commit']
    app.collections.save('notes', 'other/no-history.md', {'id': 'other', 'type': 'note'}, '')
    assert repo.log()[0]['commit'] == before
    app.close()


def test_history_revert_create_delete_and_incoming_links(tmp_path):
    from contextopus.interfaces.web.backend import LibraryBackend
    app = Application(tmp_path, discover=False)
    app.collections.index = None
    backend = LibraryBackend(app)
    repo = GitRepository(tmp_path / 'core')
    note = app.collections.save('notes', 'project/item.md', {'id': 'item', 'type': 'note'}, 'original')
    created = repo.log()[0]['commit']
    backend.history_revert(created, 'documents/notes/project/item.md')
    with pytest.raises(FileNotFoundError):
        app.collections.read('notes', 'item')
    note = app.collections.save('notes', 'project/item.md', {'id': 'item', 'type': 'note'}, 'original')
    app.collections.delete('notes', note['path'], expected_revision=note['revision'])
    deleted = repo.log()[0]['commit']
    backend.history_revert(deleted, 'documents/notes/project/item.md')
    assert app.collections.read('notes', 'item')['content'] == 'original'
    linked = app.collections.save('notes', 'project/linked.md', {'id': 'linked', 'type': 'note'}, 'linked')
    created_linked = repo.log()[0]['commit']
    app.collections.save('notes', 'project/source.md', {'id': 'source', 'type': 'note', 'references': ['[[core/documents/notes/project/linked]]']}, '')
    with pytest.raises(ValueError):
        backend.history_revert(created_linked, 'documents/notes/project/linked.md')
    assert app.collections.read('notes', linked['id'])
    app.close()


def test_history_revert_config(tmp_path):
    from contextopus.interfaces.web.backend import LibraryBackend
    app = Application(tmp_path, discover=False)
    app.collections.index = None
    backend = LibraryBackend(app)
    app.collections.save('contexts', 'project/Context.md', {'id': 'ctx', 'type': 'context'}, '')
    repo = GitRepository(tmp_path / 'core')
    backend.update_context_setting('ctx', 'documents_approval_required', True)
    commit = repo.log()[0]
    config = next(f['path'] for f in commit['files'] if f['path'].startswith('configuration/'))
    assert app.settings(module='core', context='ctx')['values']['documents_approval_required'] is True
    backend.history_revert(commit['commit'], config)
    assert app.settings(module='core', context='ctx')['values']['documents_approval_required'] is False
    app.close()
