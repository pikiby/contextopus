"""Local Git transport; modules choose eligible objects and approval policy."""

import os
from pathlib import Path
import subprocess


class GitRepository:
    def __init__(self, root):
        self.root = Path(root)

    def command(self, *args, check=True, binary=False):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(self.root),
                *args,
            ],
            env=environment,
            text=False,
            capture_output=True,
            check=False,
        )
        if not binary:
            result.stdout = result.stdout.decode("utf-8")
        result.stderr = result.stderr.decode("utf-8", errors="replace")
        if check and result.returncode:
            raise ValueError(
                result.stderr.strip() or result.stdout.strip() or "Git operation failed"
            )
        return result

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / '.git').exists():
            return
        self.command('init')
        self.command('config', 'user.name', 'Contextopus')
        self.command('config', 'user.email', 'contextopus@local')
        self.command('config', 'core.autocrlf', 'false')
        exclude = self.root / '.git/info/exclude'
        with exclude.open('a', encoding='utf-8') as stream:
            stream.write('\nanalytics/\nrag/\nstorage/\ntmp/\n')
        paths = [p for p in ('documents', 'instructions', 'configuration') if (self.root / p).exists()]
        if paths:
            self.command('add', '-A', '--', *paths)
            if self.command('diff', '--cached', '--quiet', check=False).returncode == 1:
                self.command('commit', '-m', 'Initial Contextopus history')

    def commit_all(self, message):
        try:
            existed = (self.root / '.git').exists()
            self.ensure()
            initial = self.command('rev-parse', 'HEAD', check=False) if not existed else None
            paths = [p for p in ('documents', 'instructions', 'configuration') if (self.root / p).exists() or self.command('ls-files', '--', p).stdout.strip()]
            if not paths:
                return initial.stdout.strip() if initial and not initial.returncode else None
            self.command('add', '-A', '--', *paths)
            if self.command('diff', '--cached', '--quiet', '--', *paths, check=False).returncode != 1:
                return initial.stdout.strip() if initial and not initial.returncode else None
            self.command('commit', '-m', message, '--', *paths)
            return self.command('rev-parse', 'HEAD').stdout.strip()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _commit(commit):
        import re
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{7,40}', commit):
            raise ValueError('Invalid commit')
        return commit

    @staticmethod
    def _path(path):
        from pathlib import PurePosixPath
        if not isinstance(path, str) or '\\' in path or not path or path.startswith('/'):
            raise ValueError('Invalid history path')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts) or parts[0] not in ('documents', 'instructions', 'configuration'):
            raise ValueError('Invalid history path')
        return PurePosixPath(path).as_posix()

    def log(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Invalid history limit')
        if not (self.root / '.git').exists():
            return []
        result = self.command('log', f'-{limit}', '--format=%H%x00%aI%x00%s', check=False)
        if result.returncode:
            return []
        entries = []
        for line in result.stdout.splitlines():
            commit, timestamp, subject = line.split('\0', 2)
            files = self.command('diff-tree', '--root', '--no-commit-id', '--name-status', '-z', '-r', '--no-renames', commit).stdout.split('\0')
            entries.append({'commit': commit, 'timestamp': timestamp, 'subject': subject, 'files': [{'action': files[i], 'path': files[i + 1]} for i in range(0, len(files) - 1, 2) if files[i]]})
        return entries

    def diff(self, commit, path=None):
        commit = self._commit(commit)
        args = ['show', '--format=', '--no-ext-diff', '--no-renames', commit]
        if path is not None:
            args += ['--', self._path(path)]
        return self.command(*args).stdout

    def file_at_parent(self, commit, path):
        commit, path = self._commit(commit), self._path(path)
        self.command('cat-file', '-e', commit + '^{commit}')
        result = self.command('show', commit + '~1:' + path, check=False, binary=True)
        if result.returncode:
            return None
        return result.stdout
