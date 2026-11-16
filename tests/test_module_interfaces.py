import json
from pathlib import Path
import shutil
import pytest
from contextopus.interfaces.cli.main import main


class ModuleAPI:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.state = 'discovered'

    def close(self):
        pass

    def query(self, operation, arguments=None):
        self.calls.append(('query', operation, arguments))
        if operation == 'core.modules':
            return [{'id':'sample', 'state':self.state, 'version':'1', 'open_tui':'sample.launch'}]
        if operation == 'describe':
            return {'kind':'query'}
        return {'values': {}} if operation == 'core.settings' else {'state':'idle'}

    def execute(self, operation, arguments=None):
        self.calls.append(('execute', operation, arguments))
        self.state = {'install':'enabled','prepare':'prepared','enable':'enabled','disable':'prepared','update':'enabled'}[operation.removeprefix('core.module_')]
        return {'state':self.state}


def test_cli_routes_module_commands_to_shared_operations(tmp_path, monkeypatch, capsys):
    api = ModuleAPI()
    monkeypatch.setattr('contextopus.integrations.remote.RemoteApplication', lambda *a, **kw: api)
    assert main(['--root', str(tmp_path), 'modules']) == 0
    assert json.loads(capsys.readouterr().out)[0]['id'] == 'sample'
    assert main(['--root', str(tmp_path), 'modules', 'install', 'sample']) == 0
    assert api.calls[-1] == ('execute', 'core.module_install', {'module':'sample'})
    assert json.loads(capsys.readouterr().out)['state'] == 'enabled'
    assert main(['--root', str(tmp_path), 'modules', 'update', 'sample', '--source', str(tmp_path)]) == 0
    assert api.calls[-1] == ('execute', 'core.module_update', {'module':'sample', 'source':str(tmp_path)})
