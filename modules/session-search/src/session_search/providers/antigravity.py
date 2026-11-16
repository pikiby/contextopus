"""Antigravity user input and visible planner response adapter."""
from .base import clean_dialogue_text, text_content, turn


class AntigravityProvider:
    name = "antigravity"
    default_root = "~/.gemini/antigravity/brain"

    def discover(self, root):
        yield from root.glob("*/.system_generated/logs/transcript.jsonl")

    def initial_session(self, path):
        return {"id": path.parents[2].name}

    def parse(self, record, session):
        kind = record.get("type")
        if record.get("cwd"):
            session["cwd"] = record["cwd"]
        ts = record.get("timestamp") or record.get("created_at")
        if ts and "timestamp" not in session:
            session["timestamp"] = ts
        if kind == "USER_INPUT":
            raw = text_content(record.get("content", record.get("text")))
            text = clean_dialogue_text(raw, extract_user_request=True)
            return turn("user", text, ts)
        if kind == "PLANNER_RESPONSE":
            return turn("assistant", text_content(record.get("content")), ts)
        return []
