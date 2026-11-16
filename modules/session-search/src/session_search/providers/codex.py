"""Codex response-item reader, including archived compressed transcripts."""
from .base import clean_dialogue_text, text_content, turn


class CodexProvider:
    name = "codex"
    default_root = "~/.codex"

    def discover(self, root):
        for directory in (root / "sessions", root / "archived_sessions"):
            for pattern in ("*.jsonl", "*.jsonl.zst"):
                yield from directory.rglob(pattern)

    def initial_session(self, path):
        return {"id": path.name.removesuffix(".zst").removesuffix(".jsonl")}

    def parse(self, record, session):
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        if record.get("type") == "session_meta":
            for key in ("id", "cwd", "timestamp"):
                if payload.get(key):
                    session[key] = payload[key]
        if record.get("type") == "turn_context" and payload.get("cwd"):
            session["cwd"] = payload["cwd"]
        if record.get("type") != "response_item" or payload.get("type") != "message":
            return []
        role = payload.get("role")
        if role not in ("user", "assistant") or payload.get("channel") == "analysis":
            return []
        text = text_content(payload.get("content"))
        if role == "user":
            text = clean_dialogue_text(text)
        return turn(role, text, record.get("timestamp"))
