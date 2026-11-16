"""OpenCode SQLite dialogue adapter (main sessions only, visible text parts only)."""
from datetime import datetime, timezone
import sqlite3
from .base import clean_dialogue_text


def _iso_ms(value):
    if isinstance(value, (int, float)) and value > 0:
        sec = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value) if value else None


class OpenCodeProvider:
    name = "opencode"
    default_root = "~/.local/share/opencode"
    is_sqlite = True

    def discover(self, root):
        db = root / "opencode.db"
        if db.is_file():
            yield db

    def read_db(self, db_path):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            sessions = {}
            for sid, directory, created in conn.execute(
                "SELECT id, directory, time_created FROM session WHERE parent_id IS NULL"
            ):
                sessions[sid] = (
                    {"id": sid, **({"cwd": directory} if directory else {}),
                     **({"timestamp": _iso_ms(created)} if created else {})},
                    [],
                )
            rows = conn.execute(
                "SELECT m.session_id, json_extract(m.data, '$.role'), "
                "json_extract(p.data, '$.text'), m.time_created "
                "FROM part p JOIN message m ON p.message_id = m.id "
                "WHERE json_extract(p.data, '$.type') = 'text' "
                "AND json_extract(m.data, '$.role') IN ('user', 'assistant') "
                "ORDER BY m.session_id, m.time_created, p.time_created, p.rowid"
            )
            for sid, role, raw_text, created in rows:
                entry = sessions.get(sid)
                if entry is None or not isinstance(raw_text, str):
                    continue
                text = clean_dialogue_text(raw_text) if role == "user" else raw_text.strip()
                if text:
                    pos = len(entry[1])
                    entry[1].append((pos, {"role": role, "text": text, "timestamp": _iso_ms(created)}))
            return [(meta, turns) for meta, turns in sessions.values() if turns]
        finally:
            conn.close()
