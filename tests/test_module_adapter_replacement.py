"""Native contributions participate in the common folder lifecycle."""
import pytest
from contextopus.core.operations import OperationRegistry, OperationSpec
from contextopus.startup import Application
from test_module_contributions import release


def test_native_update_refreshes_other_owner_and_survives_restart(tmp_path):
    release(tmp_path / 'modules/sample', 'one')
    with Application(tmp_path, discover=False) as first, Application(tmp_path, discover=False) as second:
        first.execute('core.module_install', {'module': 'sample'})
        assert second.query('sample.status')['title'] == 'one'
        replacement = tmp_path / 'replacement'
        release(replacement, 'two')
        first.execute('core.module_update', {'module': 'sample', 'source': str(replacement)})
        assert second.query('sample.status')['title'] == 'two'
    with Application(tmp_path, discover=False) as restored:
        assert restored.query('sample.status')['title'] == 'two'
        restored.execute('core.module_disable', {'module': 'sample'})
    with Application(tmp_path, discover=False) as restored:
        with pytest.raises(ValueError, match='Unknown operation'):
            restored.query('sample.status')


def test_failed_native_update_preserves_previous_registration(tmp_path):
    release(tmp_path / 'modules/sample', 'one')
    with Application(tmp_path, discover=False) as app:
        app.execute('core.module_install', {'module': 'sample'})
        replacement = tmp_path / 'replacement'
        release(replacement, 'two', factory='raise RuntimeError("candidate failed")\n')
        with pytest.raises(Exception, match='candidate failed'):
            app.execute('core.module_update', {'module': 'sample', 'source': str(replacement)})
        assert app.query('sample.status')['title'] == 'one'
    with Application(tmp_path, discover=False) as restored:
        assert restored.query('sample.status')['title'] == 'one'


def test_registration_conflict_is_atomic():
    registry = OperationRegistry()
    original = OperationSpec('existing', 'query', {}, lambda ctx: 'original')
    registry.register(original)
    candidate = [OperationSpec('sample.status', 'query', {}, lambda ctx: 'new'),
                 OperationSpec('existing', 'query', {}, lambda ctx: 'replacement')]
    with pytest.raises(ValueError):
        registry.replace_owned('sample', candidate)
    assert set(registry._operations) == {'existing'}
    assert registry.dispatch('query', 'existing') == 'original'
