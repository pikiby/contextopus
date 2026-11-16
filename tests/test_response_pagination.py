"""Unit tests for response budgeting, Markdown/JSON Pointer splitting, startup ordering and compact guide."""

from __future__ import annotations

import hashlib

import pytest

from contextopus.core.delivery import DeliveryManifest, DeliverySubjectManifest
from contextopus.core.operations import OperationRegistry, OperationSpec
from contextopus.core.procedure_response import enrich_procedure
from contextopus.core.response_pages import (
    BUDGET_COUNTER,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_RESPONSE_CHARS,
    JIT_TRANSITION_RESERVATION_BYTES,
    build_page,
    build_receipt,
    build_single_delivery_receipt,
    build_snapshot,
    encoded_response_size,
    escape_pointer_token,
    fits_response,
    fits_with_transition_reservation,
    needs_pagination,
    reconstruct_from_pages,
    resolve_pointer,
    split_markdown_chunks,
    split_pointer,
)


def _paginate_all(result, *, origin="test.op", context=None, root_id="root-1", delivery_manifest=None, max_bytes=DEFAULT_MAX_RESPONSE_BYTES, max_chars=DEFAULT_MAX_RESPONSE_CHARS, transitions=None):
    snap = build_snapshot(
        result,
        origin=origin,
        context=context,
        root_id=root_id,
        delivery_manifest=delivery_manifest,
        max_bytes=max_bytes,
        max_chars=max_chars,
    )
    pages = []
    for p in range(1, snap.parts_total + 1):
        tr = transitions.get(p) if transitions else None
        page = build_page(snap, p, transition=tr)
        b, c = encoded_response_size(page)
        assert b <= max_bytes
        assert c <= max_chars
        assert page["page"]["budget_counter"] == BUDGET_COUNTER
        pages.append(page)
    return snap, pages


def test_compact_guide_procedure_and_full_describe():
    reg = OperationRegistry()
    reg.register(OperationSpec("core.alpha", "query", {"type": "object", "properties": {"q": {"type": "string"}}}, lambda ctx, **kw: kw, "Alpha op"))
    reg.register(OperationSpec("workspace.beta", "execute", {"type": "object", "properties": {"id": {"type": "integer"}}}, lambda ctx, **kw: kw, "Beta op"))

    proc = enrich_procedure(
        {
            "name": "test/flow",
            "content": "---\noperations: [core.alpha, workspace.beta]\n---\nStep 1.",
        },
        reg,
        lambda n: n,
    )
    assert proc["operations"] == [
        {"operation": "core.alpha", "kind": "query", "tool": "read", "schema_call": {"tool": "guide", "operation": "core.alpha"}},
        {"operation": "workspace.beta", "kind": "execute", "tool": "write", "schema_call": {"tool": "guide", "operation": "workspace.beta"}},
    ]
    assert "arguments_schema" not in proc["operations"][0]
    assert proc["call_contract"]["paging"]
    assert "guide(operation=...)" in proc["call_contract"]["instruction"]
    assert reg.describe("workspace.beta")["arguments_schema"]["properties"]["id"] == {"type": "integer"}


def test_markdown_crlf_utf8_and_five_tier_fenced_code_splits():
    # 1. Exact CRLF + H2 outside fence vs fake ## inside code fence
    body = (
        "# Top\r\n\r\n"
        "## First Section\r\n"
        "```python\r\n"
        "## fake heading inside fence\r\n"
        + ("line = 'value'\r\n" * 60)
        + "```\r\n\r\n"
        "## Second Section\r\n"
        "Alpha paragraph.\r\n\r\n"
        "Beta paragraph.\r\n"
    )
    chunks = split_markdown_chunks(body, max_bytes=700, max_chars=700)
    assert "".join(chunk for chunk, _ in chunks) == body
    assert all(fits_response(chunk, max_bytes=700, max_chars=700) for chunk, _ in chunks)
    # Chunk splitting inside the oversized python fence must record fence_context without altering raw text
    fenced_meta = [ctx for _, ctx in chunks if ctx is not None]
    assert fenced_meta
    assert any(ctx["ends_in_fence"] or ctx["starts_in_fence"] for ctx in fenced_meta)
    assert fenced_meta[0]["fence_char"] == "`"
    assert fenced_meta[0]["fence_length"] == 3
    assert fenced_meta[0]["info"] == "python"

    # 2. H2 outside fence beats blank-line cuts when sections fit within budget
    sections = "## S1\r\n" + ("a " * 80) + "\r\n\r\n## S2\r\n" + ("b " * 80) + "\r\n\r\n## S3\r\n" + ("c " * 80) + "\r\n"
    sec_chunks = split_markdown_chunks(sections, max_bytes=450, max_chars=450)
    assert "".join(c for c, _ in sec_chunks) == sections
    assert sec_chunks[1][0].startswith("## S")

    # 3. Multibyte CJK + emoji respects byte limit strictly even when char count is low
    cjk_emoji = ("日本語🚀テスト" * 200)
    utf_chunks = split_markdown_chunks(cjk_emoji, max_bytes=250, max_chars=250)
    assert "".join(c for c, _ in utf_chunks) == cjk_emoji
    for chunk, _ in utf_chunks:
        b, c = encoded_response_size(chunk)
        assert b <= 250 and c <= 250

    # 4. Single unbroken line falls back to words -> code points cleanly
    unbroken = "X" * 1000
    tok_chunks = split_markdown_chunks(unbroken, max_bytes=200, max_chars=200)
    assert "".join(c for c, _ in tok_chunks) == unbroken
    assert all(encoded_response_size(c)[0] <= 200 for c, _ in tok_chunks)


def test_startup_ordering_toc_before_topological_doc_bodies_and_cycles():
    payload = {
        "status": "ok",
        "context": "auth",
        "extra_note": "tail-" * 600,
        "project": {
            "path": "Context.md",
            "content": "# Project Context\r\n" + ("Project fact.\r\n" * 180),
        },
        "instructions": {
            "path": "INSTRUCTIONS.md",
            "revision": "r1",
            "content": "# Primary Instructions\r\n" + ("Primary step.\r\n" * 180),
            "documents": [
                {
                    "id": "doc-app",
                    "title": "App Layer",
                    "metadata": {"depends_on": ["doc-base"]},
                    "content": "# App Doc\r\n" + ("App detail.\r\n" * 180),
                },
                {
                    "id": "doc-base",
                    "title": "Base Layer",
                    "metadata": {},
                    "content": "# Base Doc\r\n" + ("Base detail.\r\n" * 180),
                },
                {
                    "id": "doc-cycle-a",
                    "title": "Cycle A",
                    "metadata": {"depends_on": ["doc-cycle-b"]},
                    "content": "# Cycle A\r\n" + ("Cycle A line.\r\n" * 120),
                },
                {
                    "id": "doc-cycle-b",
                    "title": "Cycle B",
                    "metadata": {"depends_on": ["doc-cycle-a"]},
                    "content": "# Cycle B\r\n" + ("Cycle B line.\r\n" * 120),
                },
            ],
        },
    }

    manifest = DeliveryManifest(
        owner="workspace",
        context="auth",
        cycle_id="c1",
        scope_id="s1",
        operation="workspace.startup",
        subjects=(
            DeliverySubjectManifest("instruction", "INSTRUCTIONS.md", "r1", ("/instructions/content",), context="auth"),
            DeliverySubjectManifest("instruction", "doc-app", "ra", ("/instructions/documents/0/content",), context="auth"),
            DeliverySubjectManifest("instruction", "doc-base", "rb", ("/instructions/documents/1/content",), context="auth"),
            DeliverySubjectManifest("context", "Context.md", "rp", ("/project/content",), context="auth"),
        ),
    )

    _snap, pages = _paginate_all(payload, origin="workspace.startup", context="auth", delivery_manifest=manifest)
    assert len(pages) > 2

    flat_frags = [(idx, frag) for idx, page in enumerate(pages, start=1) for frag in page["fragments"]]
    first_page_of = {}
    for page_idx, frag in flat_frags:
        first_page_of.setdefault(frag["path"], page_idx)
    order_of_first_appearance: list[str] = []
    for _, frag in flat_frags:
        keys = [frag["path"]] if frag["kind"] != "object" else [f"{frag['path']}/{k}" if frag["path"] else f"/{k}" for k in frag["keys"]]
        for item in keys:
            if item not in order_of_first_appearance:
                order_of_first_appearance.append(item)

    # Summary scalars -> Primary -> Docs TOC (all metadata paths) -> Doc bodies (doc-base before doc-app!) -> Project -> Rest
    primary_idx = order_of_first_appearance.index("/instructions/content")
    toc_app_idx = order_of_first_appearance.index("/instructions/documents/0/id")
    toc_base_idx = order_of_first_appearance.index("/instructions/documents/1/id")
    body_base_idx = order_of_first_appearance.index("/instructions/documents/1/content")
    body_app_idx = order_of_first_appearance.index("/instructions/documents/0/content")
    project_idx = order_of_first_appearance.index("/project/content")
    rest_idx = order_of_first_appearance.index("/extra_note")

    assert primary_idx < toc_app_idx < body_base_idx
    assert primary_idx < toc_base_idx < body_app_idx
    assert toc_base_idx < body_base_idx < body_app_idx < project_idx < rest_idx

    # Lossless round-trip reconstruction preserves original list order & exact CRLF text
    reconstructed = reconstruct_from_pages(pages)
    assert reconstructed == payload


def test_rfc6901_escaping_container_packing_dict_numeric_keys_and_root_list():
    tricky = {
        "a/b": {"c~d": "value/" * 900},
        "~1": {"0": "dict-key-zero", "sub": [f"item-{i}-" + ("z" * 400) for i in range(12)]},
        "nested_all_huge": [
            {"name": f"entry-{i}", "blob": f"BLOB-{i}-" + ("q" * 2400)}
            for i in range(4)
        ],
        "empty_obj": {},
        "empty_arr": [],
    }
    assert escape_pointer_token("a/b~c") == "a~1b~0c"
    assert split_pointer("/a~1b/c~0d") == ["a/b", "c~d"]
    assert resolve_pointer(tricky, "/~01/0") == "dict-key-zero"

    _snap, pages = _paginate_all(tricky, max_bytes=3000, max_chars=3000)
    assert len(pages) >= 4
    # Verify fragmented object fragments carry keys metadata
    obj_frags = [f for p in pages for f in p["fragments"] if f["kind"] == "object"]
    assert all("keys" in f and set(f["keys"]) == set(f["value"].keys()) for f in obj_frags)
    assert reconstruct_from_pages(pages) == tricky

    # Root list with oversized items reconstructs identically
    root_list = [{"idx": i, "content": f"row-{i}\r\n" + ("R" * 1800)} for i in range(5)]
    _snap_list, list_pages = _paginate_all(root_list, max_bytes=3000, max_chars=3000)
    assert reconstruct_from_pages(list_pages) == root_list


def test_deterministic_layout_revision_and_parts_total_across_operations():
    doc_text = "## Header\r\n" + ("Payload line one.\r\n" * 140) + "\r\n## Footer\r\n" + ("Tail line.\r\n" * 140)
    expected_bytes = doc_text.encode("utf-8")
    assert len(expected_bytes) > 4096

    # Operation 1: workspace.startup embeds doc under /instructions/documents/1/content
    startup_res = {
        "status": "ok",
        "context": "proj",
        "instructions": {
            "content": "Primary",
            "documents": [
                {"id": "other", "title": "Other", "content": "Other text"},
                {"id": "guide-a", "title": "Guide A", "content": doc_text, "extra": 123},
            ],
        },
        "filler": "X" * 3000,
    }
    startup_manifest = DeliveryManifest(
        owner="workspace",
        context="proj",
        operation="workspace.startup",
        subjects=(
            DeliverySubjectManifest("instruction", "guide-a", "rev-7", ("/instructions/documents/1/content",), context="proj", metadata={"role": "doc"}),
            DeliverySubjectManifest("instruction", "ineligible-doc", "rev-x", ("/instructions/documents/0/content",), context="proj", eligible=False),
        ),
    )

    # Operation 2: instructions_read returns it at /documents/0/content
    read_res = {
        "context": "proj",
        "documents": [{"id": "guide-a", "content": doc_text}],
        "filler": "Y" * 3000,
    }
    read_manifest = DeliveryManifest(
        owner="workspace",
        context="proj",
        operation="workspace.instructions_read",
        subjects=(
            DeliverySubjectManifest("instruction", "guide-a", "rev-7", ("/documents/0/content",), context="proj", metadata={"role": "doc"}),
        ),
    )

    # Operation 3: single-page delivery (under 6000 bytes when standalone)
    small_read_res = {"id": "guide-a", "content": doc_text}
    single_manifest = DeliveryManifest(
        owner="workspace",
        context="proj",
        operation="workspace.read",
        subjects=(
            DeliverySubjectManifest("instruction", "guide-a", "rev-7", ("/content",), context="proj", metadata={"role": "doc"}),
        ),
    )

    snap1, pages1 = _paginate_all(startup_res, origin="workspace.startup", context="proj", delivery_manifest=startup_manifest)
    snap2, pages2 = _paginate_all(read_res, origin="workspace.instructions_read", context="proj", delivery_manifest=read_manifest)
    single_receipt = build_single_delivery_receipt(small_read_res, single_manifest)

    r1_subj0 = snap1.subjects[0]
    r1_inelig = snap1.subjects[1]
    r2_subj0 = snap2.subjects[0]
    r3_subj0 = single_receipt.subjects[0]

    assert r1_subj0.layout_revision == r2_subj0.layout_revision == r3_subj0.layout_revision
    assert r1_subj0.parts_total == r2_subj0.parts_total == r3_subj0.parts_total >= 3
    assert r3_subj0.issued_parts == tuple(range(1, r3_subj0.parts_total + 1))

    # Every internal part 1..parts_total is issued across paginated pages
    collected_1 = set()
    for p in range(1, snap1.parts_total + 1):
        rec = build_receipt(snap1, p)
        collected_1.update(rec.subjects[0].issued_parts)
        assert rec.subjects[0].metadata == {"role": "doc"}
        assert rec.subjects[1].issued_parts == ()
    assert collected_1 == set(range(1, r1_subj0.parts_total + 1))
    assert r1_inelig.page_parts == {}


def test_jit_transition_reservation_and_first_key_ordering():
    # ~5200 bytes fits raw (<= 6000) but must paginate when delivery_manifest reserves 1024B for JIT transition
    payload = {"status": "ok", "content": "Z" * 5200}
    assert fits_response(payload) is True
    assert fits_with_transition_reservation(payload, reserve_transition=True) is False
    assert needs_pagination(payload, reserve_transition=True) is True

    manifest = DeliveryManifest(
        owner="workspace",
        context="ctx",
        operation="workspace.read",
        subjects=(DeliverySubjectManifest("material", "m1", "r1", ("/content",), context="ctx"),),
    )
    jit = {"next_step": "continue", "remaining_parts": 1}
    assert encoded_response_size(jit)[0] <= JIT_TRANSITION_RESERVATION_BYTES

    _snap, pages = _paginate_all(payload, delivery_manifest=manifest, transitions={1: jit, 2: {"done": True}})
    assert list(pages[0].keys())[0] == "transition"
    assert pages[0]["transition"] == jit
    # Verify reconstruct_from_pages ignores transition key cleanly
    assert reconstruct_from_pages(pages) == payload

    # Oversized >1024B transition is rejected deterministically
    oversized_jit = {"blob": "B" * 1025}
    snap = build_snapshot(payload, origin="workspace.read", delivery_manifest=manifest)
    with pytest.raises(Exception) as exc_info:
        build_page(snap, 1, transition=oversized_jit)
    assert getattr(exc_info.value, "code", None) == "delivery_unavailable"


def test_verifier_edge_cases_long_keys_fences_multidigit_pages_tight_budget_next_call_nan_and_summary():
    from contextopus.core.response_pages import extract_summary

    # 1. Long key + long fence info + >=10 pages (2-digit page numbers) never overflows shell/fragment budget
    long_fenced = "```typescript-react-component-example-very-long-language-tag\n" + ("const x = 1;\n" * 180) + "```\n"
    payload = {"k" * 120: {"sub": long_fenced * 6}}
    snap, pages = _paginate_all(payload, context="ctx-edge", max_bytes=2500, max_chars=2500)
    assert snap.parts_total >= 10
    assert pages[0]["next_call"]["context"] == "ctx-edge"
    assert pages[8]["next_call"] == {
        "tool": "read",
        "operation": "core.page",
        "arguments": {"response_id": snap.response_id, "part": 10},
        "context": "ctx-edge",
    }
    assert "next_call" not in pages[-1]
    assert reconstruct_from_pages(pages) == payload

    # Without context, next_call omits "context"
    _s_no_ctx, pages_no_ctx = _paginate_all({"k": "A" * 7000})
    assert "context" not in pages_no_ctx[0]["next_call"]

    # 2. Tight custom max_bytes=2200 with ~350B summary + delivery_manifest (1024B JIT reservation)
    big_summary_payload = {
        "cycle_id": "cycle-1",
        "status": "active",
        "purpose": "test-tight-budget",
        "replayed": False,
        "task_revision": "tr-1",
        "clarification_count": 2,
        "note_a": "x" * 120,
        "note_b": "y" * 120,
        "content": "Tight-budget line.\n" * 60,
    }
    manifest = DeliveryManifest(
        owner="workspace",
        context="tight",
        operation="workspace.read",
        subjects=(DeliverySubjectManifest("material", "m-tight", "r1", ("/content",), context="tight"),),
    )
    snap_tight, pages_tight = _paginate_all(
        big_summary_payload,
        context="tight",
        delivery_manifest=manifest,
        max_bytes=2200,
        max_chars=2200,
    )
    assert snap_tight.parts_total >= 2
    assert reconstruct_from_pages(pages_tight) == big_summary_payload

    # 3. extract_summary prioritizes cycle/status/purpose/replayed/task_revision/clarification_count under 512B cap
    crowded = {f"noise_{i:02d}": f"padding-value-{i}-{'z'*25}" for i in range(15)}
    crowded.update(
        {
            "clarification_count": 3,
            "task_revision": "rev-42",
            "replayed": True,
            "purpose": "code_review",
            "status": "paused",
            "cycle_id": "cyc-99",
        }
    )
    summary = extract_summary(crowded)
    for priority_key in ("cycle_id", "status", "purpose", "replayed", "task_revision", "clarification_count"):
        assert priority_key in summary and summary[priority_key] == crowded[priority_key]

    # 4. allow_nan=False rejects NaN/Inf
    with pytest.raises(ValueError):
        encoded_response_size({"bad": float("nan")})
    with pytest.raises(ValueError):
        encoded_response_size({"bad": float("inf")})
