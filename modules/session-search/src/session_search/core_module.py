"""Thin in-process session_search adapter for Core."""

from pathlib import Path

from .indexer import MODULE, build_snapshot, scan_and_reconcile
from .store import TranscriptStore


class SessionSearchRuntime:
    """Thin Core-hosted module implementing snapshots(), status(), scan(), read()."""

    def __init__(self, core):
        self.core = core

    def _roots(self):
        return {
            "codex_home": str(Path("~/.codex").expanduser()),
            "claude_home": str(Path("~/.claude").expanduser()),
        }

    def snapshots(self):
        try:
            with self.core.storage.connect(write=False) as db:
                return [build_snapshot(db)]
        except Exception:
            return []

    def status(self):
        try:
            with self.core.storage.connect(write=False) as db:
                stats = TranscriptStore(db).stats()
        except Exception:
            stats = {"files": 0, "sessions": 0, "chunks": 0}
        return {"module": MODULE, "status": "ok", "stats": stats, "roots": self._roots()}

    def scan(self, codex_home=None, claude_home=None):
        roots = self._roots()
        ch = codex_home if codex_home is not None else roots["codex_home"]
        cl = claude_home if claude_home is not None else roots["claude_home"]
        with self.core.storage.connect(write=True) as db:
            return scan_and_reconcile(
                db,
                index=self.core.index,
                codex_home=ch,
                claude_home=cl,
            )

    def read(self, session_id, start_part=0, limit=20):
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id is required")
        with self.core.storage.connect(write=False) as db:
            return TranscriptStore(db).read_session(
                session_id, start_part=start_part, limit=limit
            )
