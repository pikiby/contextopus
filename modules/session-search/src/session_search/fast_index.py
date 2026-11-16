"""Incremental dialogue index. Parsing happens outside short write transactions."""
import io
import json
from pathlib import Path
import threading
from .providers import PROVIDERS

class DialogueIndex:
    def __init__(self, storage):
        self.storage = storage
        self.lock = threading.RLock()

    def providers(self):
        with self.storage.connect(write=False) as db:
            saved = {r[0]: (bool(r[1]), r[2]) for r in db.execute('SELECT source,enabled,root FROM session_providers')}
        return {name: {"enabled": saved.get(name, (True, p.default_root))[0],
                       "root": saved.get(name, (True, p.default_root))[1]} for name, p in PROVIDERS.items()}

    def configure(self, source, enabled=None, root=None):
        if source not in PROVIDERS:
            raise ValueError('Unknown provider')
        with self.lock:
            current = self.providers()[source]
            with self.storage.connect(write=True) as db:
                db.execute('INSERT INTO session_providers VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET enabled=excluded.enabled,root=excluded.root',
                           (source, current['enabled'] if enabled is None else enabled,
                            current['root'] if root is None else str(Path(root).expanduser().resolve())))
        return self.status()

    def status(self):
        providers = self.providers()
        with self.storage.connect(write=False) as db:
            for name, config in providers.items():
                config['sessions'] = db.execute('SELECT count(DISTINCT session_id) FROM session_files WHERE source=?', (name,)).fetchone()[0]
                config['turns'] = db.execute('SELECT count(*) FROM session_turns WHERE source=?', (name,)).fetchone()[0]
                config['pending_files'] = db.execute('SELECT count(*) FROM session_files WHERE source=? AND pending=1', (name,)).fetchone()[0]
        return {'module': 'session_search', 'status': 'ok', 'providers': providers, 'search': 'fts5', 'embeddings': False}

    def scan(self, source=None, max_bytes=8*1024*1024):
        if source is not None and source not in PROVIDERS:
            raise ValueError('Unknown provider')
        result = {'scanned_files': 0, 'updated_files': 0, 'bytes_read': 0, 'errors': []}
        with self.lock:
            for name, config in self.providers().items():
                if not config['enabled'] or source not in (None, name):
                    continue
                for path in PROVIDERS[name].discover(Path(config['root']).expanduser()):
                    result['scanned_files'] += 1
                    try:
                        if getattr(PROVIDERS[name], 'is_sqlite', False):
                            amount = self._sqlite_db(name, path)
                        else:
                            amount = self._file(name, path, max_bytes)
                        if amount is not None:
                            result['updated_files'] += 1
                            result['bytes_read'] += amount
                    except (OSError, ValueError, ImportError) as exc:
                        result['errors'].append({'path': str(path), 'error': str(exc)})
        return result

    def _file(self, source, path, budget):
        stat = path.stat()
        inode = f'{stat.st_dev}:{stat.st_ino}'
        with self.storage.connect(write=False) as db:
            old = db.execute('SELECT mtime_ns,size,offset,inode,session_json,pending FROM session_files WHERE path=?', (str(path),)).fetchone()
        if old and old[0] == stat.st_mtime_ns and old[1] == stat.st_size and not old[5]:
            return None
        compressed = path.name.endswith('.zst')
        reset = not old or old[3] != inode or stat.st_size < old[1] or (stat.st_size == old[1] and stat.st_mtime_ns != old[0]) or (compressed and stat.st_size != old[1])
        start = 0 if reset else old[2]
        provider = PROVIDERS[source]
        session = provider.initial_session(path) if reset else json.loads(old[4])
        turns = []
        offset = start
        pending = False
        with path.open('rb') as raw:
            if compressed:
                import zstandard
                stream = io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(raw))
            else:
                stream = raw
            try:
                if compressed:
                    remaining = start
                    while remaining:
                        skipped = stream.read(min(remaining, 1024*1024))
                        if not skipped:
                            raise ValueError('Compressed checkpoint exceeds file')
                        remaining -= len(skipped)
                else:
                    stream.seek(start)
                while offset - start < budget:
                    position = offset
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        pending = True
                        break
                    offset += len(line)
                    record = json.loads(line)
                    if isinstance(record, dict):
                        for turn in provider.parse(record, session):
                            turns.append((position, turn))
                else:
                    pending = True
            finally:
                if compressed:
                    stream.close()
        # Do not publish a checkpoint for a file replaced during parsing.
        after = path.stat()
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (stat.st_ino, stat.st_size, stat.st_mtime_ns):
            return None
        with self.storage.connect(write=True) as db:
            if reset:
                db.execute('DELETE FROM session_turns WHERE path=?', (str(path),))
            for position, turn in turns:
                db.execute('INSERT INTO session_turns(path,source,session_id,position,role,text,timestamp) VALUES(?,?,?,?,?,?,?)',
                           (str(path), source, session['id'], position, turn['role'], turn['text'], turn.get('timestamp')))
            db.execute('INSERT INTO session_files VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET source=excluded.source,session_id=excluded.session_id,mtime_ns=excluded.mtime_ns,size=excluded.size,offset=excluded.offset,inode=excluded.inode,session_json=excluded.session_json,pending=excluded.pending',
                       (str(path), source, session['id'], stat.st_mtime_ns, stat.st_size, offset, inode, json.dumps(session), pending))
        return offset-start

    def _sqlite_db(self, source, path):
        stat = path.stat()
        inode = f'{stat.st_dev}:{stat.st_ino}'
        prefix = f'sqlite://{path}#'
        with self.storage.connect(write=False) as db:
            old = db.execute('SELECT mtime_ns,size,inode FROM session_files WHERE source=? AND path LIKE ? LIMIT 1',
                             (source, prefix + '%')).fetchone()
        if old and old[0] == stat.st_mtime_ns and old[1] == stat.st_size and old[2] == inode:
            return None
        sessions = PROVIDERS[source].read_db(path)
        after = path.stat()
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (stat.st_ino, stat.st_size, stat.st_mtime_ns):
            return None
        with self.storage.connect(write=True) as db:
            db.execute('DELETE FROM session_turns WHERE source=? AND path LIKE ?', (source, prefix + '%'))
            db.execute('DELETE FROM session_files WHERE source=? AND path LIKE ?', (source, prefix + '%'))
            for session, turns in sessions:
                key = f"{prefix}{session['id']}"
                for position, item in turns:
                    db.execute('INSERT INTO session_turns(path,source,session_id,position,role,text,timestamp) VALUES(?,?,?,?,?,?,?)',
                               (key, source, session['id'], position, item['role'], item['text'], item.get('timestamp')))
                db.execute('INSERT INTO session_files VALUES(?,?,?,?,?,?,?,?,?)',
                           (key, source, session['id'], stat.st_mtime_ns, stat.st_size, stat.st_size, inode, json.dumps(session), False))
        return stat.st_size

    def _sources(self, source):
        settings = self.providers()
        if source is not None and source not in settings:
            raise ValueError('Unknown provider')
        return [name for name, config in settings.items() if config['enabled'] and source in (None, name)]

    def list(self, source=None, cwd=None, since=None, limit=20):
        names = self._sources(source)
        if not names:
            return {'sessions': []}
        conditions = ['f.source IN (' + ','.join('?' for _ in names) + ')',
                      'EXISTS (SELECT 1 FROM session_turns t WHERE t.path=f.path)']
        args = list(names)
        if cwd is not None:
            conditions.append("json_extract(f.session_json,'$.cwd')=?")
            args.append(cwd)
        if since is not None:
            conditions.append('f.mtime_ns>=CAST(strftime(\'%s\',?) AS INTEGER)*1000000000')
            args.append(since)
        with self.storage.connect(write=False) as db:
            rows = db.execute('SELECT f.source,f.session_id,f.session_json,f.mtime_ns, '
                '(SELECT text FROM session_turns t WHERE t.path=f.path AND role=\'user\' ORDER BY position LIMIT 1), '
                '(SELECT text FROM session_turns t WHERE t.path=f.path AND role=\'user\' ORDER BY position DESC LIMIT 1) '
                'FROM session_files f WHERE ' + ' AND '.join(conditions) + ' ORDER BY f.mtime_ns DESC LIMIT ?', (*args, limit)).fetchall()
        return {'sessions': [{'source': r[0], 'session_id': r[1], **json.loads(r[2]), 'mtime_ns': r[3], 'first_prompt': r[4], 'last_prompt': r[5]} for r in rows]}

    def read(self, session_id, source=None, start_part=0, limit=20, tail=False):
        names = self._sources(source)
        with self.storage.connect(write=False) as db:
            sources = [r[0] for r in db.execute('SELECT DISTINCT source FROM session_turns WHERE session_id=?', (session_id,)) if r[0] in names]
            if len(sources) > 1:
                raise ValueError('Session id is ambiguous; specify source')
            if not sources:
                return {'session_id': session_id, 'parts': [], 'total_parts': 0}
            total = db.execute('SELECT count(*) FROM session_turns WHERE session_id=? AND source=?', (session_id, sources[0])).fetchone()[0]
            offset = max(0, total-limit) if tail else start_part
            rows = db.execute('SELECT role,text,timestamp FROM session_turns WHERE session_id=? AND source=? ORDER BY position,id LIMIT ? OFFSET ?', (session_id, sources[0], limit, offset)).fetchall()
        return {'session_id': session_id, 'source': sources[0], 'total_parts': total,
                'parts': [{'part': offset+i, 'role': r[0], 'text': r[1], 'timestamp': r[2]} for i, r in enumerate(rows)]}

    def find(self, query, source=None, limit=20):
        names = self._sources(source)
        if not names:
            return {'matches': []}
        with self.storage.connect(write=False) as db:
            rows = db.execute('SELECT t.source,t.session_id,t.role,t.text,t.position FROM session_fts JOIN session_turns t ON t.id=session_fts.rowid WHERE session_fts MATCH ? AND t.source IN (' + ','.join('?' for _ in names) + ') ORDER BY rank LIMIT ?', (query, *names, limit)).fetchall()
        return {'matches': [{'source': r[0], 'session_id': r[1], 'role': r[2], 'text': r[3], 'position': r[4]} for r in rows]}
