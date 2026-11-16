"""Thin SQLite state store for transcript checkpoints and extracted chunks."""

import json
import time


class TranscriptStore:
    def __init__(self, connection):
        self.db = connection

    def get_file_state(self, path):
        row = self.db.execute(
            'SELECT source, session_id, offset, mtime_ns, size_bytes, state_json, session_json '
            'FROM "session_search::transcript_state" WHERE path = ?',
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        return {
            "source": row[0],
            "session_id": row[1],
            "offset": row[2],
            "mtime_ns": row[3],
            "size_bytes": row[4],
            "state": json.loads(row[5] or "{}"),
            "session": json.loads(row[6] or "{}"),
        }

    def upsert_file_state(self, *, path, source, session_id, offset, mtime_ns, size_bytes, state, session):
        self.db.execute(
            'INSERT INTO "session_search::transcript_state" '
            '(path, source, session_id, offset, mtime_ns, size_bytes, state_json, session_json, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(path) DO UPDATE SET '
            'source=excluded.source, session_id=excluded.session_id, offset=excluded.offset, '
            'mtime_ns=excluded.mtime_ns, size_bytes=excluded.size_bytes, '
            'state_json=excluded.state_json, session_json=excluded.session_json, updated_at=excluded.updated_at',
            (
                str(path),
                source,
                session_id,
                offset,
                mtime_ns,
                size_bytes,
                json.dumps(state),
                json.dumps(session),
                time.time(),
            ),
        )

    def upsert_chunks(self, *, source, path, chunks, session):
        for chunk in chunks:
            metadata = {
                "type": "session_block",
                "source": source,
                "session_id": chunk["session_id"],
                "part": chunk["part"],
                "start_offset": chunk["start_offset"],
                "end_offset": chunk["end_offset"],
                "cwd": session.get("cwd"),
                "timestamp": session.get("timestamp"),
                "archived": "archived_sessions" in str(path),
            }
            self.db.execute(
                'INSERT INTO "session_search::chunks" '
                '(id, session_id, source, path, part, start_offset, end_offset, text_hash, text, metadata_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET '
                'start_offset=excluded.start_offset, end_offset=excluded.end_offset, '
                'text_hash=excluded.text_hash, text=excluded.text, metadata_json=excluded.metadata_json',
                (
                    chunk["id"],
                    chunk["session_id"],
                    source,
                    str(path),
                    chunk["part"],
                    chunk["start_offset"],
                    chunk["end_offset"],
                    chunk["text_hash"],
                    chunk["text"],
                    json.dumps(metadata),
                ),
            )

    def all_chunks(self):
        rows = self.db.execute(
            'SELECT id, session_id, source, path, text_hash, text, metadata_json '
            'FROM "session_search::chunks" ORDER BY session_id, part'
        ).fetchall()
        return [
            {
                "id": r[0],
                "session_id": r[1],
                "source": r[2],
                "path": r[3],
                "text_hash": r[4],
                "text": r[5],
                "metadata": json.loads(r[6] or "{}"),
            }
            for r in rows
        ]

    def read_session(self, session_id, *, start_part=0, limit=20):
        rows = self.db.execute(
            'SELECT id, source, path, part, start_offset, end_offset, text, metadata_json '
            'FROM "session_search::chunks" WHERE session_id = ? AND part >= ? ORDER BY part LIMIT ?',
            (session_id, start_part, limit),
        ).fetchall()
        total = self.db.execute(
            'SELECT COUNT(*) FROM "session_search::chunks" WHERE session_id = ?',
            (session_id,),
        ).fetchone()[0]
        return {
            "session_id": session_id,
            "total_parts": total,
            "parts": [
                {
                    "id": r[0],
                    "source": r[1],
                    "path": r[2],
                    "part": r[3],
                    "start_offset": r[4],
                    "end_offset": r[5],
                    "text": r[6],
                    "metadata": json.loads(r[7] or "{}"),
                }
                for r in rows
            ],
        }

    def stats(self):
        files = self.db.execute('SELECT COUNT(*) FROM "session_search::transcript_state"').fetchone()[0]
        chunks = self.db.execute('SELECT COUNT(*) FROM "session_search::chunks"').fetchone()[0]
        sessions = self.db.execute('SELECT COUNT(DISTINCT session_id) FROM "session_search::chunks"').fetchone()[0]
        return {"files": files, "sessions": sessions, "chunks": chunks}
