"""Small provider contract and dialogue-cleaning helpers shared by session adapters."""
from pathlib import Path
import re
from typing import Iterable, Protocol

_WRAPPER_TAGS = (
    "INSTRUCTIONS",
    "environment_context",
    "permissions instructions",
    "collaboration_mode",
    "user_instructions",
    "turn_aborted",
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "task-notification",
    "teammate-message",
    "CONTEXT_SUMMARY",
    "ADDITIONAL_METADATA",
    "USER_SETTINGS_CHANGE",
    "SYSTEM_MESSAGE",
)
_TAG_PATTERNS = [
    re.compile(rf"<{re.escape(tag)}\b[^>]*>.*?</{re.escape(tag)}>", re.S | re.I)
    for tag in _WRAPPER_TAGS
]
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S | re.I)
_AGENTS_HEADER = re.compile(r"^#\s*AGENTS\.md instructions for [^\n]*\n*", re.M)


class Provider(Protocol):
    name: str
    default_root: str
    def discover(self, root: Path) -> Iterable[Path]: ...
    def initial_session(self, path: Path) -> dict: ...
    def parse(self, record: dict, session: dict) -> list[dict]: ...


def text_content(value, types=("text", "input_text", "output_text")):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(item["text"] for item in value
                         if isinstance(item, dict) and item.get("type") in types
                         and isinstance(item.get("text"), str)).strip()
    return ""


def clean_dialogue_text(text: str, *, extract_user_request: bool = False) -> str:
    """Strip client wrapper/instruction tags so only real chat dialogue remains."""
    if not text:
        return ""
    if extract_user_request:
        match = _USER_REQUEST.search(text)
        if match:
            return match.group(1).strip()
    cleaned = text
    for pattern in _TAG_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _AGENTS_HEADER.sub("", cleaned)
    return cleaned.strip()


def turn(role, text, timestamp=None):
    return [{"role": role, "text": text, "timestamp": timestamp}] if text else []
