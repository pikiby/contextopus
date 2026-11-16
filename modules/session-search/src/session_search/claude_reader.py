"""Incremental canonical Claude Code conversation extraction; no inference dependencies."""

import copy
import hashlib
import json
import re
from pathlib import Path

from .reader import BLOCK_TOKENS, BLOCK_VERSION

_CLAUDE_CONTEXT = re.compile(
    r"<(system-reminder|local-command-caveat|command-name|command-message|command-args|local-command-stdout)\b[^>]*>.*?</\1>",
    re.S | re.I,
)


def _clean_claude_text(text):
    return _CLAUDE_CONTEXT.sub("", text).strip()


def _extract_message_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return _clean_claude_text(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                cleaned = _clean_claude_text(item["text"])
                if cleaned:
                    parts.append(cleaned)
        return "\n".join(parts)
    return ""


def read_claude_increment(
    path,
    offset=0,
    state=None,
    *,
    max_chars=None,
    max_tokens=BLOCK_TOKENS,
    token_counter=None,
    max_bytes=1024 * 1024,
):
    """Incrementally extract canonical user/assistant text blocks from a Claude Code JSONL file."""
    max_chars = max_chars if max_chars is not None else max_tokens * 8
    if max_chars < 64 or max_tokens < 16 or offset < 0 or max_bytes < 1:
        raise ValueError("Invalid chunk limit or offset")
    state = copy.deepcopy(state or {})
    state.setdefault("session", {})
    state.setdefault("block_number", 0)
    chunks, errors = {}, []
    initial_offset = offset

    def fits(text):
        return len(text) <= max_chars and (not token_counter or token_counter(text) <= max_tokens)

    def publish(final=False):
        tail = state.get("tail")
        if not tail:
            return
        tail["final"] = final
        tail["text_hash"] = hashlib.sha256(tail["text"].encode()).hexdigest()
        chunks[tail["id"]] = copy.deepcopy(tail)
        if final:
            state.pop("tail")
            state["block_number"] += 1

    def append(text, role, start, end):
        consumed = 0
        while text:
            tail = state.get("tail")
            base = tail["text"] + "\n" if tail else ""
            label = role.capitalize() + ": "
            candidate = base + label + text
            size = len(text)
            if not fits(candidate):
                lo, hi = 0, min(len(text), max_chars)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fits(base + label + text[:mid]):
                        lo = mid
                    else:
                        hi = mid - 1
                size = lo
                if not size:
                    if tail:
                        publish(final=True)
                        continue
                    raise ValueError("Chunk budget cannot hold one character and role")
                boundary = max(text.rfind(" ", 0, size), text.rfind("\n", 0, size))
                if boundary >= size * 3 // 4:
                    size = boundary + 1
                while size and not fits(base + label + text[:size]):
                    size -= 1
                if not size:
                    raise ValueError("Chunk budget cannot hold token boundary")
            if not tail:
                sid = state["session"]["id"]
                key = f"claude:{sid}:{BLOCK_VERSION}:{state['block_number']}"
                tail = {
                    "id": hashlib.sha256(key.encode()).hexdigest(),
                    "session_id": sid,
                    "start_offset": start,
                    "end_offset": end,
                    "part": state["block_number"],
                    "role": "conversation",
                    "text": "",
                    "sources": [],
                }
                state["tail"] = tail
            tail["text"] = base + label + text[:size]
            tail["end_offset"] = end
            tail["sources"].append(
                {
                    "role": role,
                    "start_offset": start,
                    "end_offset": end,
                    "char_start": consumed,
                    "char_end": consumed + size,
                }
            )
            consumed += size
            text = text[size:]
            publish(final=bool(text))

    path_obj = Path(path)
    with open(path_obj, "rb") as stream:
        stream.seek(offset)
        while offset - initial_offset < max_bytes:
            start = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            end = stream.tell()
            offset = end
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                errors.append({"start_offset": start, "end_offset": end, "error": "invalid_json"})
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind not in ("user", "assistant"):
                continue
            if item.get("isMeta") or item.get("isSidechain"):
                continue
            sid = item.get("sessionId") or path_obj.stem
            if not state["session"].get("id"):
                state["session"] = {
                    "id": sid,
                    "source": "claude_code",
                    "timestamp": item.get("timestamp"),
                    "cwd": item.get("cwd"),
                    "gitBranch": item.get("gitBranch"),
                }
            text = _extract_message_text(item.get("message"))
            if not text:
                continue
            append(text, kind, start, end)
    return {
        "session": state["session"],
        "next_offset": offset,
        "state": state,
        "chunks": list(chunks.values()),
        "errors": errors,
    }
