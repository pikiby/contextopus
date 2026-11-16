"""End-to-end verification of Core workflows and session search."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from contextopus.core.documents import (
    ContextReader,
    DocumentCollections,
    DocumentFileStore,
    DocumentMutations,
    LinkCodec,
    collect_relations,
)
from contextopus.core.documents.library import browse_documents
from contextopus.core.rag import DocumentKey
from contextopus.core.settings import Settings
from contextopus.integrations.skill_delivery import delivery_options, entrypoints
from contextopus.startup import Application


def _make_doc(path: Path, meta: dict, body: str) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{header}\n---\n\n{body.strip()}\n", encoding="utf-8")


def test_collections_links_context_read_and_mutations(tmp_path):
    root = tmp_path / "core_root"
    app = Application(root, discover=False)
    try:
        app.initialize()
        docs_root = root / "core" / "documents"

        _make_doc(
            root / "core" / "instructions" / "rule-a.md",
            {"id": "rule-a", "type": "instruction", "title": "Rule A", "description": "Core rule"},
            "Always verify outputs.\nSee [[knowledge/note-1|Note 1]].",
        )
        _make_doc(
            docs_root / "knowledge" / "note-1.md",
            {"id": "note-1", "type": "note", "title": "Note 1", "description": "Architecture note"},
            "Design details here.",
        )
        _make_doc(
            docs_root / "contexts" / "proj-x.md",
            {
                "id": "proj-x",
                "type": "context",
                "title": "Project X",
                "description": "Test project",
                "requires_rule": ["core/instructions/rule-a"],
            },
            "Work on Project X.",
        )

        # Phase 1: collections discovery
        cols = {c["id"] for c in app.collections.collections()}
        assert cols >= {"contexts", "instructions", "knowledge"}
        assert app.collections.list("contexts")[0]["id"] == "proj-x"

        # Phase 2: relations
        rel = app.query("core.documents_relations", {"collection": "knowledge", "id": "note-1"})
        assert any(
            item["key"] == {"module": "core", "collection": "instructions", "id": "rule-a"}
            for item in rel["incoming"]
        )

        # Phase 3: context_read
        ctx = app.query("core.context_read", {"id": "proj-x"})
        assert ctx["context"]["id"] == "proj-x"
        assert "settings" in ctx
        assert any(r["id"] == "rule-a" for r in ctx["rules"])

        # Phase 4: preview + save without approval for default knowledge/notes, and required approval for instructions
        preview = app.query(
            "core.documents_preview",
            {
                "collection": "knowledge",
                "path": "note-2.md",
                "metadata": {"id": "note-2", "type": "note", "title": "Note 2", "description": "New note"},
                "content": "Fresh content.",
            },
        )
        assert preview["approval_required"] is False
        assert preview["approval_template"] is None
        saved = app.execute(
            "core.documents_save",
            {
                "collection": "knowledge",
                "path": "note-2.md",
                "metadata": {"id": "note-2", "type": "note", "title": "Note 2", "description": "New note"},
                "content": "Fresh content.",
            },
        )
        assert saved["id"] == "note-2"
        assert saved["revision"]

        inst_preview = app.query(
            "core.documents_preview",
            {
                "collection": "instructions",
                "path": "rule-b.md",
                "metadata": {"id": "rule-b", "type": "instruction", "title": "Rule B", "description": "Second rule"},
                "content": "Rule B body.",
            },
        )
        assert inst_preview["approval_required"] is True
        assert set(inst_preview["approval_template"]) == {"change_revision", "confirmation"}
        inst_saved = app.execute(
            "core.documents_save",
            {
                "collection": "instructions",
                "path": "rule-b.md",
                "metadata": {"id": "rule-b", "type": "instruction", "title": "Rule B", "description": "Second rule"},
                "content": "Rule B body.",
                "approval": {"change_revision": inst_preview["change_revision"], "confirmation": "yes"},
            },
        )
        assert inst_saved["id"] == "rule-b"

        # Deleting note-1 must block because rule-a links to it
        with pytest.raises(ValueError, match="incoming links"):
            app.execute(
                "core.documents_delete",
                {
                    "collection": "knowledge",
                    "path": "note-1.md",
                    "expected_revision": app.collections.read("knowledge", "note-1")["revision"],
                    "approval": {"confirmation": "yes"},
                },
            )

        # Phase 5: core.library cross-collection browse
        lib_cols = app.query("core.library", {"action": "collections"})
        assert any(c["id"] == "contexts" for c in lib_cols["collections"])
        lib_rec = app.query("core.library", {"action": "record", "collection": "instructions", "id": "rule-a"})
        assert any(lk["key"] == ["core", "knowledge", "note-1"] for lk in lib_rec["links"])
    finally:
        app.close()


def test_settings_protected_lowering_and_per_context_override(tmp_path):
    root = tmp_path / "settings_root"
    app = Application(root, discover=False)
    try:
        app.initialize()
        cfg_ctx = root / "core" / "configuration" / "contexts" / "core" / "proj-x.json"
        cfg_ctx.parent.mkdir(parents=True, exist_ok=True)
        cfg_ctx.write_text(json.dumps({"material_max_tokens": 2048}), encoding="utf-8")

        settings = Settings(app.layout)
        eff = settings.effective_for_context("proj-x")
        assert eff["material_max_tokens"] == 2048
        assert eff["documents_approval_required"] is False
        assert eff["instructions_approval_required"] is True

        # Raising per-context documents_approval_required works, and lowering requires explicit confirmation
        prev_up = app.query(
            "core.settings_preview",
            {"changes": {"documents_approval_required": True}, "module": "core", "context": "proj-x"},
        )
        assert app.execute("core.settings_apply", {"preview_revision": prev_up["preview_revision"]})["saved"] is True
        assert settings.effective_for_context("proj-x")["documents_approval_required"] is True

        # Lowering protected setting (instructions_approval_required) requires explicit confirmation
        prev = app.query("core.settings_preview", {"changes": {"instructions_approval_required": False}})
        assert prev["requires_confirmation"] is True
        with pytest.raises(ValueError, match="Confirmation"):
            app.execute("core.settings_apply", {"preview_revision": prev["preview_revision"]})
        applied = app.execute(
            "core.settings_apply",
            {"preview_revision": prev["preview_revision"], "confirmation": "yes"},
        )
        assert applied["saved"] is True
    finally:
        app.close()


def test_single_w_skill_and_no_finish_in_router_or_mcp(tmp_path):
    root = tmp_path / "router_root"
    app = Application(root, discover=False)
    try:
        assert "core.finish" not in app.registry._operations
        assert "core.context_read" in app.registry._operations
        guide_default = app.procedure("default")
        assert any(r["id"] == "work" for r in guide_default["routes"])
        work_proc = app.procedure("work")
        assert "core.context_read" in work_proc["content"]

        # Skill delivery delivers "w" directly without contextopus- prefix
        opts = delivery_options({}, selected=["w"])
        entries = entrypoints([("w", Path("SKILL.md"))], opts, plugin=False)
        assert entries == [("w", "w", Path("SKILL.md"))]

        from contextopus.interfaces.mcp.server import create_server

        server = create_server(root)
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert {"guide", "read", "write"} <= tool_names
        assert "finish" not in tool_names
    finally:
        app.close()


def test_session_search_codex_and_claude_readers(tmp_path):
    from session_search.indexer import scan_and_reconcile
    from session_search.store import TranscriptStore

    codex_home = tmp_path / ".codex"
    codex_file = codex_home / "sessions" / "2026" / "s1.jsonl"
    codex_file.parent.mkdir(parents=True)
    codex_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "codex-1", "cwd": "/repo", "timestamp": "2026-11-15T10:00:00Z"}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix bug in rag.py"}]}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Patched scopes filter."}]}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    claude_home = tmp_path / ".claude"
    claude_file = claude_home / "projects" / "proj-a" / "claude-1.jsonl"
    claude_file.parent.mkdir(parents=True)
    claude_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "sessionId": "claude-1", "cwd": "/repo", "timestamp": "2026-11-15T11:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ignore</system-reminder>Check claude_reader"}]}}),
                json.dumps({"type": "assistant", "sessionId": "claude-1", "cwd": "/repo", "timestamp": "2026-11-15T11:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Claude reader is ready."}]}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "session_search.sqlite3"
    conn = sqlite3.connect(db_path)
    sql_path = (
        Path(__file__).resolve().parents[1]
        / "modules"
        / "session-search"
        / "module"
        / "migrations"
        / "001-transcript-state.sql"
    )
    conn.executescript(sql_path.read_text(encoding="utf-8").replace("{{module}}", "session_search"))

    res = scan_and_reconcile(conn, index=None, codex_home=codex_home, claude_home=claude_home)
    assert res["scanned_files"] == 2
    assert res["updated_files"] == 2
    assert res["stats"]["sessions"] == 2
    assert res["stats"]["chunks"] == 2

    store = TranscriptStore(conn)
    cl_read = store.read_session("claude-1")
    assert len(cl_read["parts"]) == 1
    assert "ignore" not in cl_read["parts"][0]["text"]
    assert "Check claude_reader" in cl_read["parts"][0]["text"]
    conn.close()


def test_per_context_policy_settings_scope_route_map_and_preview_receipt(tmp_path):
    from contextopus.core.operations import OperationInputError
    from contextopus.startup import Application

    root = tmp_path / "policy_root"
    app = Application(root, discover=False)
    try:
        app.initialize()
        app.collections.save(
            "instructions",
            "rule-p.md",
            {"id": "rule-p", "type": "instruction", "title": "Rule P", "description": "Mandatory rule"},
            "Keep things verified.",
        )
        app.collections.save(
            "knowledge",
            "proj-a/std.md",
            {"id": "std-1", "type": "standard", "title": "Mouth Standard", "description": "How to build mouth"},
            "Carve inward.",
        )
        app.collections.save(
            "contexts",
            "proj-a/Context.md",
            {
                "id": "ctx-uuid-a",
                "type": "context",
                "slug": "proj-a",
                "title": "Project A",
                "description": "Project A context",
                "requires_rule": ["[[core/instructions/rule-p]]"],
                "rule_routes": [{"when": "Editing mouth geometry", "target": "[[core/documents/knowledge/proj-a/std]]"}],
            },
            "Project A body. " + ("Context detail line. " * 220),
        )

        # Root defaults to documents_approval_required=False, instructions_approval_required=True;
        # per-context can override in BOTH directions (enable True when root False, or disable False when root True).
        (root / "core" / "configuration" / "settings.json").write_text(
            json.dumps({"documents_approval_required": True, "instructions_approval_required": True}),
            encoding="utf-8",
        )
        ctx_cfg = root / "core" / "configuration" / "contexts" / "core" / "ctx-uuid-a.json"
        ctx_cfg.parent.mkdir(parents=True, exist_ok=True)
        ctx_cfg.write_text(json.dumps({"documents_approval_required": False}), encoding="utf-8")

        scoped = Application(root, context="proj-a", discover=False)
        try:
            snap = scoped.query("core.settings", {})
            assert snap["selected_scope"]["context"] == "ctx-uuid-a"
            assert snap["values"]["documents_approval_required"] is False
            assert snap["sources"]["documents_approval_required"] == "context.configuration"
            short = scoped.query("core.settings", {"fields": ["documents_approval_required"]})
            assert short["values"] == {"documents_approval_required": False}

            # Context can turn approval back ON, then lower it back to False with confirmation
            up = scoped.query("core.settings_preview", {"changes": {"documents_approval_required": True}, "context": "proj-a"})
            assert scoped.execute("core.settings_apply", {"preview_revision": up["preview_revision"]})["saved"] is True
            assert scoped.query("core.settings", {})["values"]["documents_approval_required"] is True

            down = scoped.query("core.settings_preview", {"changes": {"documents_approval_required": False}, "context": "proj-a"})
            assert down["requires_confirmation"] is True
            assert scoped.execute(
                "core.settings_apply",
                {"preview_revision": down["preview_revision"], "confirmation": "allow auto-write for proj-a"},
            )["saved"] is True
            assert scoped.query("core.settings", {})["values"]["documents_approval_required"] is False

            # context_read returns top settings + resolved route_map and fits >6KB single delivery without paging
            ctx_res = scoped.query("core.context_read", {"id": "proj-a"})
            assert list(ctx_res.keys())[0] == "settings"
            assert ctx_res["route_map"][0]["when"] == "Editing mouth geometry"
            assert ctx_res["route_map"][0]["mandatory_when_matching"] is True
            delivered = scoped.finalize_delivery(ctx_res, operation="core.context_read", kind="query")
            assert delivered.get("status") != "paginated"
            assert delivered["route_map"][0]["target"] == "[[core/documents/knowledge/proj-a/std]]"
        finally:
            scoped.close()

        # Unknown approval keys (like approved=True) are rejected cleanly by schema validation
        with pytest.raises(OperationInputError):
            app.execute(
                "core.documents_save",
                {
                    "collection": "instructions",
                    "path": "rule-x.md",
                    "metadata": {"id": "rule-x", "type": "instruction", "title": "Rule X", "description": "Desc"},
                    "content": "Body",
                    "approval": {"approved": True},
                },
            )

        # Save without calling core.documents_preview fails when approval is required (instructions or root True)
        with pytest.raises(ValueError, match="Preview required"):
            app.execute(
                "core.documents_save",
                {
                    "collection": "instructions",
                    "path": "rule-x.md",
                    "metadata": {"id": "rule-x", "type": "instruction", "title": "Rule X", "description": "Desc"},
                    "content": "Body",
                    "approval": {"change_revision": "0" * 64, "confirmation": "yes"},
                },
            )

        prev = app.query(
            "core.documents_preview",
            {
                "collection": "knowledge",
                "path": "proj-other/new.md",
                "metadata": {"id": "new-1", "type": "note", "title": "New", "description": "Desc"},
                "content": "Body",
            },
        )
        assert prev["approval_required"] is True
        assert prev["approval_template"] == {
            "change_revision": prev["change_revision"],
            "confirmation": "<explicit user approval text>",
        }
        assert "action_contract" in prev
        assert prev["policy_source"] == "root.configuration"
        saved = app.execute(
            "core.documents_save",
            {
                "collection": "knowledge",
                "path": "proj-other/new.md",
                "metadata": {"id": "new-1", "type": "note", "title": "New", "description": "Desc"},
                "content": "Body",
                "approval": {"change_revision": prev["change_revision"], "confirmation": "approved exact diff"},
            },
        )
        assert saved["saved"] is True

        # Delete preview + delete flow
        del_prev = app.query(
            "core.documents_preview",
            {
                "collection": "knowledge",
                "path": "proj-other/new.md",
                "expected_revision": saved["revision"],
                "action": "delete",
            },
        )
        assert del_prev["action"] == "delete"
        assert set(del_prev["approval_template"]) == {"change_revision", "confirmation"}
        deleted = app.execute(
            "core.documents_delete",
            {
                "collection": "knowledge",
                "path": "proj-other/new.md",
                "expected_revision": saved["revision"],
                "approval": {"change_revision": del_prev["change_revision"], "confirmation": "delete confirmed"},
            },
        )
        assert deleted["deleted"] is True
    finally:
        app.close()
