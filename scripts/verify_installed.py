"""Verify an already installed candidate using temporary data, without model calls.

Run with the clean environment's interpreter. This script does not install,
start an OS service, modify native-client configuration or access a user context.
"""
import argparse
import atexit
import asyncio
from importlib import metadata
from importlib.util import find_spec
from importlib.resources import files
import inspect
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

def planned_skills(root):
    """Inspect a native integration preview without writing client configuration."""
    from contextopus.integrations import plan
    preview = plan('codex', root / 'client-preview', root=root)
    return {Path(item['path']).parent.name for item in preview['writes']
            if Path(item['path']).name == 'SKILL.md'}


def verify(expected_version, prefix=None):
    from contextopus.startup import Application
    from contextopus.interfaces.mcp.server import create_server
    from contextopus.core.filtering import AlgorithmRegistry

    installed = metadata.version('contextopus-core')
    if installed != expected_version:
        raise ValueError(f'Expected {expected_version}, installed {installed}')
    source = Path(inspect.getfile(Application)).resolve()
    executable = Path(sys.executable).parent / ('contextopus.exe' if os.name == 'nt' else 'contextopus')
    if prefix is not None and (not source.is_relative_to(Path(prefix).resolve())
                               or not executable.resolve().is_relative_to(Path(prefix).resolve())):
        raise ValueError('The installed check resolved outside the clean installation prefix')
    cli = subprocess.run([str(executable), '--version'], text=True, capture_output=True, check=True).stdout.strip()
    if cli != 'Contextopus ' + expected_version:
        raise ValueError('CLI version differs from installed package')
    subprocess.run([str(executable), '--help'], check=True, stdout=subprocess.DEVNULL)
    resources = files('contextopus').joinpath('resources')
    skills = sorted(path.name for path in resources.joinpath('skills').iterdir() if path.is_dir())
    if skills != ['w']:
        raise ValueError('Wheel must supply the w skill')
    algorithms = AlgorithmRegistry().catalog()
    if len(algorithms) != 5 or any(row['version'] != '2.0.0' for row in algorithms):
        raise ValueError('Installed search algorithm inventory is stale')
    with tempfile.TemporaryDirectory(prefix='contextopus-installed-check-') as temporary:
        temporary = Path(temporary)
        root = temporary / 'core-only'
        with Application(root, discover=False) as app:
            if app.query('describe', {'operation': 'core.module_install'})['kind'] != 'execute':
                raise ValueError('Installed module operation registry is stale')
            if planned_skills(root) != {'w'}:
                raise ValueError('Integration preview must contain the w skill')
            if app.resources.providers._entries:
                raise ValueError('Resource inspection unexpectedly loaded an embedding model')
        from contextopus.interfaces.mcp import server
        from unittest.mock import patch
        owner = server.ServerOwner(root)
        try:
            with patch.object(server, 'ServerOwner', return_value=owner):
                mcp = create_server(root)
            tools = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
            if tools != ['guide', 'read', 'write']:
                raise ValueError('Installed default MCP surface is not guide/read/write')
        finally:
            atexit.unregister(owner.close)
            owner.close()
    return {'version': installed, 'source': str(source), 'cli': cli, 'tools': tools,
            'core_skills': skills, 'core_only_verified': True, 'algorithms': len(algorithms),
            'model_loaded': False,
            'scope': 'temporary_data_only', 'model_behavior_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--prefix', type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.expected_version, arguments.prefix), indent=2))
