"""Incremental transcript scanner and Core RAG reconciler for session_search."""

from contextopus.core.documents import SourceSnapshot, reconcile_index
from contextopus.core.rag import DocumentKey, IndexDocument

from .sources import discover_transcripts, read_transcript_increment
from .store import TranscriptStore

MODULE = "session_search"
COLLECTION = "transcripts"


def build_snapshot(connection):
    """Build a Core SourceSnapshot from stored transcript chunks."""
    store = TranscriptStore(connection)
    docs = []
    for chunk in store.all_chunks():
        docs.append(
            IndexDocument(
                key=DocumentKey(MODULE, COLLECTION, chunk["id"]),
                revision=chunk["text_hash"],
                text=chunk["text"],
                location=chunk["path"],
                metadata=chunk["metadata"],
            )
        )
    return SourceSnapshot(module=MODULE, collection=COLLECTION, documents=docs, complete=True)


def scan_and_reconcile(connection, index=None, *, codex_home=None, claude_home=None):
    """Incrementally read new/changed transcripts and reconcile into Core RAG."""
    store = TranscriptStore(connection)
    transcripts = discover_transcripts(codex_home=codex_home, claude_home=claude_home)
    updated_files = 0
    new_chunks = 0
    errors = []

    for source_kind, path in transcripts:
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        prev = store.get_file_state(path)
        if prev and prev["mtime_ns"] == stat.st_mtime_ns and prev["size_bytes"] == stat.st_size:
            continue
        offset = prev["offset"] if prev and stat.st_size >= prev["offset"] else 0
        state = prev["state"] if prev and offset > 0 else None
        try:
            res = read_transcript_increment(source_kind, path, offset=offset, state=state)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        session = res.get("session") or {}
        sid = session.get("id") or path.stem
        chunks = res.get("chunks") or []
        if chunks:
            store.upsert_chunks(source=source_kind, path=path, chunks=chunks, session=session)
            new_chunks += len(chunks)
        store.upsert_file_state(
            path=path,
            source=source_kind,
            session_id=sid,
            offset=res["next_offset"],
            mtime_ns=stat.st_mtime_ns,
            size_bytes=stat.st_size,
            state=res["state"],
            session=session,
        )
        updated_files += 1
        if res.get("errors"):
            errors.extend({"path": str(path), **err} for err in res["errors"])

    snapshot = build_snapshot(connection)
    rag_status = None
    if index is not None:
        rag_status = reconcile_index(index, snapshot)
    return {
        "scanned_files": len(transcripts),
        "updated_files": updated_files,
        "chunks_updated": new_chunks,
        "errors": errors,
        "rag": rag_status,
        "stats": store.stats(),
    }
