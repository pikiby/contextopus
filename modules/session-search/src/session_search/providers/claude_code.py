"""Claude Code visible dialogue only, without tool results, thinking, or subagents."""
from .base import clean_dialogue_text, text_content, turn


class ClaudeProvider:
    name = "claude_code"
    default_root = "~/.claude/projects"

    def discover(self, root):
        yield from root.rglob("*.jsonl")

    def initial_session(self, path):
        return {"id": path.stem}

    def parse(self, record, session):
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isMeta") or record.get("isSidechain"):
            return []
        for key, field in (("id", "sessionId"), ("cwd", "cwd"), ("timestamp", "timestamp")):
            if record.get(field):
                session[key] = record[field]
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        text = clean_dialogue_text(text_content(message.get("content"), ("text",)))
        return turn(role, text, record.get("timestamp"))
