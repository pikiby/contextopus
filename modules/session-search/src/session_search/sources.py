"""Transcript discovery and reader dispatch across Codex and Claude Code."""

from pathlib import Path

from .claude_reader import read_claude_increment
from .reader import read_increment


def discover_transcripts(*, codex_home=None, claude_home=None):
    """Yield (source_kind, path) tuples for all readable transcript JSONL files."""
    found = []
    if codex_home:
        base = Path(codex_home).expanduser().resolve()
        for sub in ("sessions", "archived_sessions"):
            root = base / sub
            if root.is_dir():
                for path in sorted(root.rglob("*.jsonl")):
                    if path.is_file():
                        found.append(("codex", path))
    if claude_home:
        base = Path(claude_home).expanduser().resolve()
        projects = base / "projects" if (base / "projects").is_dir() else base
        if projects.is_dir():
            for path in sorted(projects.rglob("*.jsonl")):
                if path.is_file():
                    found.append(("claude_code", path))
    return found


def read_transcript_increment(source_kind, path, offset=0, state=None, **kwargs):
    """Dispatch incremental transcript extraction by source kind."""
    if source_kind == "claude_code":
        return read_claude_increment(path, offset=offset, state=state, **kwargs)
    if source_kind == "codex":
        return read_increment(path, offset=offset, state=state, **kwargs)
    raise ValueError("Unsupported transcript source: " + str(source_kind))
