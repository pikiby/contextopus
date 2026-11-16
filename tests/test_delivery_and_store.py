"""Unit tests for core.delivery, ResponseStore durability/errors, ContributionHost & relocation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest

from contextopus.core.delivery import (
    DELIVERY_MANIFEST_KEY,
    CoreStructuredError,
    DeliveryManifest,
    DeliveryReceipt,
    DeliveryResult,
    DeliverySubjectManifest,
    attach_delivery_manifest,
    capture_delivery_manifest,
)
from contextopus.core.operations import OperationRegistry, OperationSpec
from contextopus.core.relocation import _transient
from contextopus.core.response_pages import build_page, build_snapshot, encoded_response_size, fits_response
from contextopus.core.response_store import ResponseStore, finalize_operation_delivery
from contextopus.core.storage import StorageLayout
from contextopus.startup import Application


def _make_manifest(owner="sample", operation="sample.read", path="/content"):
    return DeliveryManifest(
        owner=owner,
        context=None,
        cycle_id="c1",
        scope_id="s1",
        operation=operation,
        subjects=(DeliverySubjectManifest("material", "m1", "r1", (path,)),),
    )


def test_operation_registry_manifest_capture_isolation_and_owner_check():
    reg = OperationRegistry()

    def inner_op(ctx, **_kw):
        return attach_delivery_manifest({"inner": True}, _make_manifest(owner="other", operation="other.inner"))

    def outer_op(ctx, call_inner=False, attach_foreign=False, **_kw):
        if call_inner:
            inner_res = reg.dispatch("query", "other.inner", {}, ctx)
            assert DELIVERY_MANIFEST_KEY not in inner_res
        base = {"ok": True, "text": "hello"}
        if attach_foreign:
            return attach_delivery_manifest(base, _make_manifest(owner="other"))
        return attach_delivery_manifest(base, _make_manifest(owner="sample"))

    reg.register_module_adapter = None
    reg.replace_owned("sample", [OperationSpec("sample.read", "query", {"type": "object"}, outer_op, "Outer")])
    reg.replace_owned("other", [OperationSpec("other.inner", "query", {"type": "object"}, inner_op, "Inner")])

    # Direct dispatch strips internal key cleanly
    direct = reg.dispatch("query", "sample.read", {}, None)
    assert DELIVERY_MANIFEST_KEY not in direct

    # Capture grabs ONLY outermost manifest, never leaked inner manifest
    with capture_delivery_manifest() as cap:
        res = reg.dispatch("query", "sample.read", {"call_inner": True}, None)
        assert DELIVERY_MANIFEST_KEY not in res
    assert cap.manifest is not None
    assert cap.manifest.owner == "sample"

    # Cross-module manifest forgery rejected
    with pytest.raises(ValueError, match="does not match operation owner"):
        with capture_delivery_manifest():
            reg.dispatch("query", "sample.read", {"attach_foreign": True}, None)

    # core.page next_call skips automatic OperationRegistry definition injection
    page_reg = OperationRegistry()
    page_reg.register(
        OperationSpec(
            "core.page",
            "query",
            {"type": "object", "properties": {"response_id": {"type": "string"}, "part": {"type": "integer"}}},
            lambda ctx, response_id, part: {
                "status": "paginated",
                "page": {"response_id": response_id, "part": part, "parts_total": 2},
                "next_call": {"tool": "read", "operation": "core.page", "arguments": {"response_id": response_id, "part": 2}},
            },
            "Page op",
        )
    )
    page_res = page_reg.dispatch("query", "core.page", {"response_id": "a" * 32, "part": 1}, None)
    assert "definition" not in page_res["next_call"]


def test_response_store_permissions_lazy_creation_ttl_and_cleanup(tmp_path):
    now = [1000.0]
    layout = StorageLayout(tmp_path)
    store = ResponseStore(layout, clock=lambda: now[0])

    # Initializing and missing reads MUST NOT create runtime/response-pages
    with pytest.raises(CoreStructuredError) as missing_exc:
        store.fetch_page("a" * 32, 1)
    assert missing_exc.value.code == "response_not_found"
    assert missing_exc.value.execution_started is False
    assert not (tmp_path / "runtime" / "response-pages").exists()

    snap1 = build_snapshot({"text": "A" * 7000}, origin="test.op", root_id=store.root_id)
    store.save(snap1)

    cache_dir = tmp_path / "runtime" / "response-pages"
    json_file = cache_dir / f"{snap1.response_id}.json"
    lock_file = cache_dir / f"{snap1.response_id}.lock"
    if os.name != "nt":
        assert (cache_dir.stat().st_mode & 0o777) == 0o700
        assert (json_file.stat().st_mode & 0o777) == 0o600
        assert (lock_file.stat().st_mode & 0o777) == 0o600
    assert not any(p.name.endswith(".tmp") for p in cache_dir.iterdir())

    # Reading before TTL does NOT extend expires_at
    now[0] = 1000.0 + 50000.0
    p2 = store.fetch_page(snap1.response_id, 2)
    assert p2["page"]["part"] == 2
    saved_record = json.loads(json_file.read_text(encoding="utf-8"))
    assert saved_record["expires_at"] == pytest.approx(1000.0 + 86400.0)

    # Save second snapshot at t = 1000 + 86000
    now[0] = 1000.0 + 86000.0
    snap2 = build_snapshot({"text": "B" * 7000}, origin="test.op", root_id=store.root_id)
    store.save(snap2)

    # Advance past snap1 TTL (1000 + 86400), snap2 still valid -> save(snap3) lazily deletes expired snap1
    now[0] = 1000.0 + 86401.0
    snap3 = build_snapshot({"text": "C" * 7000}, origin="test.op", root_id=store.root_id)
    store.save(snap3)
    assert not json_file.exists()
    assert not lock_file.exists()
    assert (cache_dir / f"{snap2.response_id}.json").exists()

    # Advance past snap2 TTL and fetch snap2 directly -> response_expired & removes snap2 files
    now[0] = 1000.0 + 86000.0 + 86400.0
    with pytest.raises(CoreStructuredError) as expired_exc:
        store.fetch_page(snap2.response_id, 1)
    assert expired_exc.value.code == "response_expired"
    assert not (cache_dir / f"{snap2.response_id}.json").exists()


def test_response_store_full_error_matrix_and_symlinks(tmp_path):
    now = [2000.0]
    layout = StorageLayout(tmp_path)
    store = ResponseStore(layout, clock=lambda: now[0])
    snap = build_snapshot({"text": "X" * 7000}, origin="workspace.read", context="ctx-a", root_id=store.root_id)
    store.save(snap)
    cache_dir = tmp_path / "runtime" / "response-pages"
    json_path = cache_dir / f"{snap.response_id}.json"

    # invalid_arguments
    for bad_id in ("not-hex", "A" * 32, "../" + "a" * 29, "a" * 31):
        with pytest.raises(CoreStructuredError) as exc:
            store.fetch_page(bad_id, 1, expected_context="ctx-a")
        assert exc.value.code == "invalid_arguments"
    with pytest.raises(CoreStructuredError) as bad_part:
        store.fetch_page(snap.response_id, 0, expected_context="ctx-a")
    assert bad_part.value.code == "invalid_arguments"

    # response_context_mismatch
    for wrong_ctx in ("ctx-b", None):
        with pytest.raises(CoreStructuredError) as exc:
            store.fetch_page(snap.response_id, 1, expected_context=wrong_ctx)
        assert exc.value.code == "response_context_mismatch"

    # response_part_out_of_range
    with pytest.raises(CoreStructuredError) as exc:
        store.fetch_page(snap.response_id, snap.parts_total + 1, expected_context="ctx-a")
    assert exc.value.code == "response_part_out_of_range"

    # response_root_mismatch
    other_store = ResponseStore(layout, root_id="/other/root", clock=lambda: now[0])
    with pytest.raises(CoreStructuredError) as exc:
        other_store.fetch_page(snap.response_id, 1, expected_context="ctx-a")
    assert exc.value.code == "response_root_mismatch"

    # response_format_unsupported
    raw_valid = json.loads(json_path.read_text(encoding="utf-8"))
    bad_ver = {**raw_valid, "format_version": 99}
    json_path.write_text(json.dumps(bad_ver), encoding="utf-8")
    with pytest.raises(CoreStructuredError) as exc:
        store.fetch_page(snap.response_id, 1, expected_context="ctx-a")
    assert exc.value.code == "response_format_unsupported"

    # response_corrupt: integrity digest mismatch
    tampered = dict(raw_valid)
    tampered["snapshot"] = {**raw_valid["snapshot"], "origin": "tampered.op"}
    json_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CoreStructuredError) as exc:
        store.fetch_page(snap.response_id, 1, expected_context="ctx-a")
    assert exc.value.code == "response_corrupt"

    # response_corrupt: invalid JSON
    json_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(CoreStructuredError) as exc:
        store.fetch_page(snap.response_id, 1, expected_context="ctx-a")
    assert exc.value.code == "response_corrupt"

    # response_corrupt: symlink entry
    real_target = tmp_path / "outside.json"
    real_target.write_text(json.dumps(raw_valid), encoding="utf-8")
    sym_id = "f" * 32
    os.symlink(real_target, cache_dir / f"{sym_id}.json")
    with pytest.raises(CoreStructuredError) as exc:
        store.fetch_page(sym_id, 1, expected_context="ctx-a")
    assert exc.value.code == "response_corrupt"


def test_response_cache_failed_preserves_execution_started_and_receipt(tmp_path):
    layout = StorageLayout(tmp_path)
    # Create a file where runtime directory would be so save() fails deterministically
    (tmp_path / "runtime").write_text("blocker-file", encoding="utf-8")
    store = ResponseStore(layout)
    with pytest.raises(CoreStructuredError) as exc:
        finalize_operation_delivery(
            {"status": "ok", "content": "W" * 7000},
            operation="workspace.write_op",
            kind="execute",
            context=None,
            response_store=store,
            delivery_handlers={},
        )
    err = exc.value.payload["error"]
    assert err["code"] == "response_cache_failed"
    assert err["execution_started"] is True
    assert err["operation"] == "workspace.write_op"
    assert err["receipt"]["part"] == 1
    assert err["receipt"]["parts_total"] >= 2
    assert len(err["receipt"]["response_id"]) == 32


def test_response_store_releases_lock_before_delivery_handler_under_concurrency(tmp_path):
    layout = StorageLayout(tmp_path)
    seen_receipts = []

    def handler(receipt: DeliveryReceipt) -> DeliveryResult:
        # Acquires root guard INSIDE delivery_handler while concurrent callers hit ResponseStore
        with layout.guard():
            seen_receipts.append((receipt.part, receipt.complete, receipt.subjects[0].issued_parts))
            return DeliveryResult(accepted=True, transition={"step": f"p{receipt.part}"})

    store = ResponseStore(layout, delivery_dispatcher=handler)
    manifest = _make_manifest(owner="sample")
    page1 = finalize_operation_delivery(
        {"content": "L" * 7000},
        operation="sample.read",
        kind="query",
        context=None,
        response_store=store,
        delivery_handlers={"sample": handler},
        delivery_manifest=manifest,
    )
    rid = page1["page"]["response_id"]
    total = page1["page"]["parts_total"]
    assert list(page1.keys())[0] == "transition"
    assert page1["transition"] == {"step": "p1"}

    def worker(part_num: int):
        # Also test acquiring layout.guard while saving/reading other entries concurrently
        other_snap = build_snapshot({"content": f"K{part_num}" * 4000}, origin="sample.other", root_id=store.root_id)
        with layout.guard():
            store.save(other_snap)
        return store.fetch_page(rid, part_num)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(worker, range(2, total + 1)))

    assert len(pages) == total - 1
    assert all(p["transition"]["step"].startswith("p") for p in pages)
    assert any(part == total and complete is True for part, complete, _ in seen_receipts)


def test_contribution_host_delivery_handler_lifecycle_and_relocation_transient(tmp_path):
    mod_dir = tmp_path / "modules" / "sample"
    pkg_dir = mod_dir / "package" / "sample"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        '''
from contextopus.core.delivery import DeliveryResult, DeliverySubjectManifest, attach_delivery_manifest, DeliveryManifest

RECEIPTS = []

def register(host):
    def handle_delivery(receipt):
        RECEIPTS.append(receipt)
        return DeliveryResult(accepted=True, transition={"from": "sample", "part": receipt.part})

    host.delivery_handler("sample", handle_delivery)

    def big_op(text):
        return attach_delivery_manifest(
            {"content": text},
            DeliveryManifest(
                owner="sample",
                context=None,
                operation="sample.big",
                subjects=(DeliverySubjectManifest("material", "m1", "r1", ("/content",)),),
            ),
        )

    host._operation("sample.big", "query", big_op, {"text": (str, "default")})
''',
        encoding="utf-8",
    )
    (mod_dir / "module.json").write_text(
        json.dumps({"id": "sample", "version": "1.0", "contribution": {"factory": "sample/__init__.py:register"}}),
        encoding="utf-8",
    )

    with Application(tmp_path, discover=False) as app:
        assert not (tmp_path / "runtime" / "response-pages").exists()
        app.execute("core.module_prepare", {"module": "sample"})
        app.execute("core.module_enable", {"module": "sample"})
        assert "sample" in app.delivery_handlers

        # Python app.query strips manifest without calling delivery_handler
        raw = app.query("sample.big", {"text": "small"})
        assert DELIVERY_MANIFEST_KEY not in raw

        # Finalize delivery calls delivery_handler & attaches JIT transition first
        with capture_delivery_manifest() as cap:
            raw2 = app.query("sample.big", {"text": "Z" * 7000})
        page1 = app.finalize_delivery(raw2, operation="sample.big", kind="query", delivery_manifest=cap.manifest)
        assert list(page1.keys())[0] == "transition"
        assert page1["transition"] == {"from": "sample", "part": 1}

        rid = page1["page"]["response_id"]
        page2 = app.query("core.page", {"response_id": rid, "part": 2})
        assert list(page2.keys())[0] == "transition"
        assert page2["transition"] == {"from": "sample", "part": 2}
        assert fits_response(page1) and fits_response(page2)

        # Transient rule excludes runtime/response-pages files from relocation
        assert _transient(Path("runtime/response-pages")) is True
        assert _transient(Path(f"runtime/response-pages/{rid}.json")) is True
        assert _transient(Path(f"runtime/response-pages/{rid}.lock")) is True

        # Disable unregisters handler cleanly -> next core.page returns delivery_unavailable
        app.execute("core.module_disable", {"module": "sample"})
        assert "sample" not in app.delivery_handlers
        with pytest.raises(CoreStructuredError) as unavail:
            app.query("core.page", {"response_id": rid, "part": 1})
        assert unavail.value.code == "delivery_unavailable"
        assert unavail.value.execution_started is False
