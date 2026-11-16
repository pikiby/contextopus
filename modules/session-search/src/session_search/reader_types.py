"""Canonical types for incremental session transcript extraction."""

from dataclasses import dataclass, field


@dataclass
class SessionMetadata:
    id: str
    source: str
    path: str
    cwd: str | None = None
    timestamp: str | None = None
    git_branch: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class TranscriptChunk:
    id: str
    session_id: str
    source: str
    path: str
    part: int
    start_offset: int
    end_offset: int
    text: str
    text_hash: str
    final: bool = False
