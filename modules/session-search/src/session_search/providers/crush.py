"""Charm Crush SQLite dialogue adapter (main sessions only, visible text parts only)."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from .base import clean_dialogue_text


def _iso_ts(value):
    if isinstance(value, (int, float)) and value > 0:
        sec = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value) if value else None


class CrushProvider:
    name = "crush"
    default_root = "~/.local/share/crush"
    is_sqlite = True

    def discover(self, root):
        seen = set()
        direct = root / "crush.db"
        if direct.is_file():
            seen.add(direct.resolve())
            yield direct
        projects = root / "projects.json"
        if projects.is_file():
            try:
                data = json.loads(projects.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            for item in data.get("projects") or []:
                data_dir = item.get("data_dir")
                if not data_dir:
                    continue
                db = Path(data_dir).expanduser() / "crush.db"
                if db.is_file() and db.resolve() not in seen:
                    seen.add(db.resolve())
                    yield db

    def read_db(self, db_path):
        project_cwd = str(db_path.parent.parent) if db_path.parent.name == ".crush" else None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            sessions = {}
            for sid, created in conn.execute(
                "SELECT id, created_at FROM sessions WHERE COALESCE(parent_session_id, '') = ''"
            ):
                meta = {"id": sid}
                if project_cwd:
                    meta["cwd"] = project_cwd
                if created:
                    meta["timestamp"] = _iso_ts(created)
                sessions[sid] = (meta, [])
            rows = conn.execute(
                "SELECT session_id, role, parts, created_at FROM messages "
                "WHERE COALESCE(is_summary_message, 0) = 0 AND role IN ('user', 'assistant') "
                "ORDER BY session_id, created_at, rowid"
            )
            for sid, role, raw_parts, created in rows:
                entry = sessions.get(sid)
                if entry is None or not raw_parts:
                    continue
                try:
                    parts = json.loads(raw_parts)
                except ValueError:
                    continue
                if not isinstance(parts, list):
                    continue
                chunks = []
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        txt = (part.get("data") or {}).get("text")
                        if isinstance(txt, str) and txt.strip():
                            chunks.append(txt.strip())
                text = "\n".join(chunks).strip()
                if role == "user":
                    text = clean_dialogue_text(text)
                if text:
                    pos = len(entry[1])
                    entry[1].append((pos, {"role": role, "text": text, "timestamp": _iso_ts(created)}))
            return [(meta, turns) for meta, turns in sessions.values() if turns]
        finally:
            conn.close()
