"""Real streamable-http integration tests for guide, core.page, RemoteApplication and module delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from contextopus.core.response_pages import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_RESPONSE_CHARS,
    encoded_response_size,
    reconstruct_from_pages,
)
from contextopus.integrations.remote import RemoteApplication


def _error_code(res) -> str:
    assert res.isError
    text = res.content[0].text
    return json.loads(text[text.index("{"):])["error"]["code"]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_health(port: int) -> None:
    import time

    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2):
                return
        except Exception:
            time.sleep(0.05)
    raise AssertionError(f"HTTP server on port {port} did not become healthy")


def _install_sample_module(root: Path) -> None:
    mod_dir = root / "modules" / "sample"
    pkg_dir = mod_dir / "package" / "sample"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        '''
from contextopus.core.delivery import (
    DeliveryManifest,
    DeliveryResult,
    DeliverySubjectManifest,
    attach_delivery_manifest,
)

def register(host):
    seen_parts = []

    def handle_delivery(receipt):
        seen_parts.append(
            {
                "part": receipt.part,
                "parts_total": receipt.parts_total,
                "complete": receipt.complete,
                "issued": list(receipt.subjects[0].issued_parts),
                "layout_revision": receipt.subjects[0].layout_revision,
            }
        )
        return DeliveryResult(
            accepted=True,
            transition={
                "step": f"part-{receipt.part}-of-{receipt.parts_total}",
                "complete": receipt.complete,
            },
        )

    host.delivery_handler("sample", handle_delivery)

    def read_large(size):
        text = "## Intro\\r\\n" + ("Line of markdown content.\\r\\n" * (size // 27 + 5))
        return attach_delivery_manifest(
            {"status": "ok", "doc_id": "d1", "content": text},
            DeliveryManifest(
                owner="sample",
                context=None,
                cycle_id="c-http",
                scope_id="s-http",
                operation="sample.read_large",
                subjects=(
                    DeliverySubjectManifest("material", "d1", "rev-1", ("/content",)),
                ),
            ),
        )

    def write_large(size):
        counter_file = host.layout.root / "write_count.txt"
        count = int(counter_file.read_text(encoding="utf-8")) + 1 if counter_file.exists() else 1
        counter_file.write_text(str(count), encoding="utf-8")
        text = "Write output block.\\n" * (size // 20 + 5)
        return {"status": "written", "exec_count": count, "content": text}

    def check_close(note="ok", cancel=False):
        return {"status": "ready", "close_arguments": {"note": note}}

    def do_close(note="ok", cancel=False):
        close_counter = host.layout.root / "close_count.txt"
        count = int(close_counter.read_text(encoding="utf-8")) + 1 if close_counter.exists() else 1
        close_counter.write_text(str(count), encoding="utf-8")
        (host.layout.root / "closed_marker.txt").write_text("closed", encoding="utf-8")
        return {
            "status": "closed",
            "exec_count": count,
            "report": "Finish close summary line.\\r\\n" * 350,
        }

    def receipts():
        return {"events": list(seen_parts)}

    host.procedure(
        "sample/large-guide",
        lambda: {
            "content": "---\\noperations: [sample.read_large, sample.write_large]\\n---\\n"
            + ("## Step Section\\r\\n" + ("Detailed guide instructions.\\r\\n" * 60)) * 5,
        },
    )

    host._operation("sample.read_large", "query", read_large, {"size": (int, 9000)})
    host._operation("sample.write_large", "execute", write_large, {"size": (int, 9000)})
    host._operation("sample.check_close", "query", check_close, {"note": (str, "ok"), "cancel": (bool, False)})
    host._operation("sample.do_close", "execute", do_close, {"note": (str, "ok"), "cancel": (bool, False)})
    host._operation("sample.receipts", "query", receipts, {})
''',
        encoding="utf-8",
    )
    (mod_dir / "module.json").write_text(
        json.dumps(
            {
                "id": "sample",
                "version": "1.0",
                "contribution": {"factory": "sample/__init__.py:register"},

            }
        ),
        encoding="utf-8",
    )


def _start_http_server(root: Path, port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from contextopus.interfaces.mcp.server import run; import sys; run(sys.argv[1], port=int(sys.argv[2]))",
            str(root),
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_health(port)
    except Exception:
        process.terminate()
        process.wait(timeout=5)
        raise
    return process


def test_http_guide_pagination_remote_modes_and_delivery_lifecycle(tmp_path):
    _install_sample_module(tmp_path)
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}/mcp"
    proc = _start_http_server(tmp_path, port)
    try:
        remote = RemoteApplication(tmp_path, endpoint=endpoint)
        remote_follow = RemoteApplication(tmp_path, endpoint=endpoint, follow_pages=True)
        try:
            remote.execute("core.module_prepare", {"module": "sample"})
            remote.execute("core.module_enable", {"module": "sample"})

            # 1. guide() catalog & procedure & schema lookup + catalog pagination over MCP HTTP
            async def check_mcp_direct():
                async with streamable_http_client(endpoint) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        g_proc = await session.call_tool("guide", {"procedure": "modules"})
                        assert not g_proc.isError
                        raw_text = g_proc.content[0].text
                        assert len(raw_text.encode("utf-8")) <= DEFAULT_MAX_RESPONSE_BYTES
                        assert len(raw_text) <= DEFAULT_MAX_RESPONSE_CHARS

                        g_page_schema = await session.call_tool("guide", {"operation": "core.page"})
                        assert not g_page_schema.isError
                        schema_payload = json.loads(g_page_schema.content[0].text)
                        assert schema_payload["operation"] == "core.page"
                        assert "response_id" in schema_payload["arguments_schema"]["properties"]

                        # Catalog fits cleanly under 6000 bytes and lists core.page
                        g_cat = await session.call_tool("guide", {"catalog": True})
                        assert not g_cat.isError
                        assert len(g_cat.content[0].text.encode("utf-8")) <= DEFAULT_MAX_RESPONSE_BYTES
                        cat_payload = json.loads(g_cat.content[0].text)
                        if cat_payload.get("status") == "paginated":
                            cat_pages = [cat_payload]
                            for p in range(2, cat_payload["page"]["parts_total"] + 1):
                                np = await session.call_tool("read", {"operation": "core.page", "arguments": {"response_id": cat_payload["page"]["response_id"], "part": p}})
                                cat_pages.append(json.loads(np.content[0].text))
                            cat_payload = reconstruct_from_pages(cat_pages)
                        assert "core.page" in cat_payload["operations"]["query"]

                        # Oversized contributed guide procedure paginates over core.page cleanly
                        g_big = await session.call_tool("guide", {"procedure": "sample/large-guide"})
                        assert not g_big.isError
                        big_first = json.loads(g_big.content[0].text)
                        assert len(g_big.content[0].text.encode("utf-8")) <= DEFAULT_MAX_RESPONSE_BYTES
                        assert big_first["status"] == "paginated"
                        assert "definition" not in big_first["next_call"]
                        pages = [big_first]
                        rid = big_first["page"]["response_id"]
                        for p in range(2, big_first["page"]["parts_total"] + 1):
                            nxt = await session.call_tool(
                                "read",
                                {"operation": "core.page", "arguments": {"response_id": rid, "part": p}},
                            )
                            assert not nxt.isError
                            raw_nxt = nxt.content[0].text
                            assert len(raw_nxt.encode("utf-8")) <= DEFAULT_MAX_RESPONSE_BYTES
                            assert len(raw_nxt) <= DEFAULT_MAX_RESPONSE_CHARS
                            decoded_nxt = json.loads(raw_nxt)
                            if "next_call" in decoded_nxt:
                                assert "definition" not in decoded_nxt["next_call"]
                            pages.append(decoded_nxt)
                        big_full = reconstruct_from_pages(pages)
                        assert "sample.read_large" in [o["operation"] for o in big_full["operations"]]

                        # Context mismatch check when reading same response under a named context
                        mismatch = await session.call_tool(
                            "read",
                            {
                                "operation": "core.page",
                                "context": "other-ws",
                                "arguments": {"response_id": rid, "part": 2},
                            },
                        )
                        assert _error_code(mismatch) == "response_context_mismatch"

                        # Out-of-range page & missing response_id & invalid_arguments & expired
                        oor = await session.call_tool(
                            "read",
                            {
                                "operation": "core.page",
                                "arguments": {"response_id": rid, "part": big_first["page"]["parts_total"] + 1},
                            },
                        )
                        assert _error_code(oor) == "response_part_out_of_range"

                        from contextopus.core.response_pages import build_snapshot
                        from contextopus.core.response_store import ResponseStore
                        from contextopus.core.storage import StorageLayout

                        expired_id = "e" * 32
                        expired_store = ResponseStore(StorageLayout(tmp_path), clock=lambda: 1000.0)
                        expired_snap = build_snapshot(
                            {"content": "E" * 7000},
                            origin="test.expired",
                            root_id=expired_store.root_id,
                            response_id=expired_id,
                        )
                        expired_store.save(expired_snap)
                        expired_call = await session.call_tool(
                            "read",
                            {"operation": "core.page", "arguments": {"response_id": expired_id, "part": 1}},
                        )
                        assert _error_code(expired_call) == "response_expired"

                        missing = await session.call_tool(
                            "read",
                            {"operation": "core.page", "arguments": {"response_id": "0" * 32, "part": 1}},
                        )
                        assert _error_code(missing) == "response_not_found"

                        bad_arg = await session.call_tool(
                            "read",
                            {"operation": "core.page", "arguments": {"response_id": "BAD", "part": 1}},
                        )
                        assert _error_code(bad_arg) == "invalid_arguments"

            asyncio.run(check_mcp_direct())

            # 2. RemoteApplication default follow_pages=False returns Page 1 without auto-collecting
            first_page = remote.query("sample.read_large", {"size": 9000})
            assert remote.is_page(first_page) is True
            assert list(first_page.keys())[0] == "transition"
            assert first_page["transition"] == {
                "step": f"part-1-of-{first_page['page']['parts_total']}",
                "complete": False,
            }
            assert first_page["page"]["parts_total"] >= 3
            assert all(f >= 0 for f in encoded_response_size(first_page))
            b1, c1 = encoded_response_size(first_page)
            assert b1 <= DEFAULT_MAX_RESPONSE_BYTES and c1 <= DEFAULT_MAX_RESPONSE_CHARS

            # collect_pages and query_all / execute_all reconstruct full payloads
            collected = remote.collect_pages(first_page)
            via_follow = remote_follow.query("sample.read_large", {"size": 9000})
            via_query_all = remote.query_all("sample.read_large", {"size": 9000})
            assert collected == via_follow == via_query_all
            assert collected["content"].startswith("## Intro\r\n")

            # Verify delivery_handler received receipts for every page in sequence (1..N)
            receipts = remote.query("sample.receipts")["events"]
            parts_seen = [ev["part"] for ev in receipts]
            assert parts_seen == [1, *range(2, first_page["page"]["parts_total"] + 1), *range(1, first_page["page"]["parts_total"] + 1), *range(1, first_page["page"]["parts_total"] + 1)]
            last_ev = receipts[-1]
            assert last_ev["complete"] is True
            assert len({ev["layout_revision"] for ev in receipts}) == 1

            # Oversized execute/write call runs handler ONCE, paginates & reconstructs across follow_pages=True / execute_all
            write_first = remote.execute("sample.write_large", {"size": 9000})
            assert remote.is_page(write_first) is True
            assert (tmp_path / "write_count.txt").read_text(encoding="utf-8") == "1"
            write_collected = remote.collect_pages(write_first)
            assert (tmp_path / "write_count.txt").read_text(encoding="utf-8") == "1"
            assert write_collected["exec_count"] == 1

            write_follow = remote_follow.execute("sample.write_large", {"size": 9000})
            assert write_follow["exec_count"] == 2
            assert (tmp_path / "write_count.txt").read_text(encoding="utf-8") == "2"

            # Direct MCP detail="full" call is STILL strictly bounded & paginates cleanly
            async def check_detail_full_bounded():
                async with streamable_http_client(endpoint) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        full_res = await session.call_tool(
                            "read",
                            {
                                "operation": "sample.read_large",
                                "arguments": {"size": 9000},
                                "detail": "full",
                            },
                        )
                        assert not full_res.isError
                        raw = full_res.content[0].text
                        assert len(raw.encode("utf-8")) <= DEFAULT_MAX_RESPONSE_BYTES
                        assert len(raw) <= DEFAULT_MAX_RESPONSE_CHARS
                        assert json.loads(raw)["status"] == "paginated"

            asyncio.run(check_detail_full_bounded())

            # Keep an uncollected read_large response + pending write_large response across server restart
            write_across_restart = remote.execute("sample.write_large", {"size": 9000})
            assert (tmp_path / "write_count.txt").read_text(encoding="utf-8") == "3"
            read_across_close = remote.query("sample.read_large", {"size": 9000})

            # Earlier paginated reads remain available.
            read_post_close = remote.collect_pages(read_across_close)
            assert read_post_close["content"].startswith("## Intro\r\n")
        finally:
            remote.close()
            remote_follow.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # Server restart between write page 1 and core.page remaining parts
    port2 = _free_port()
    endpoint2 = f"http://127.0.0.1:{port2}/mcp"
    proc2 = _start_http_server(tmp_path, port2)
    try:
        remote2 = RemoteApplication(tmp_path, endpoint=endpoint2)
        try:
            write_reconstructed = remote2.collect_pages(write_across_restart)
            assert write_reconstructed["status"] == "written"
            assert write_reconstructed["exec_count"] == 3
            assert (tmp_path / "write_count.txt").read_text(encoding="utf-8") == "3"

            # After server restart, disabling module triggers delivery_unavailable cleanly
            remote2.execute("core.module_disable", {"module": "sample"})
            with pytest.raises(RuntimeError, match="delivery_unavailable"):
                remote2.page(read_across_close["page"]["response_id"], 1)
        finally:
            remote2.close()
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)


def test_cli_constructs_remote_with_follow_pages_true(tmp_path, monkeypatch):
    from contextopus.interfaces.cli import main as cli_main

    captured = {}

    class StubRemote:
        def __init__(self, root, context=None, endpoint="http://127.0.0.1:8765/mcp", *, legacy_tools=False, follow_pages=False):
            captured["root"] = root
            captured["context"] = context
            captured["follow_pages"] = follow_pages
            self.services = type("Services", (), {"status": staticmethod(lambda: {"state": "running"})})()

        def close(self):
            captured["closed"] = True

    import contextopus.integrations.remote as remote_mod
    import contextopus.integrations.service as service_mod

    monkeypatch.setattr(remote_mod, "RemoteApplication", StubRemote)
    monkeypatch.setattr(service_mod, "status", lambda root: {"state": "running"})

    rc = cli_main.main(["--root", str(tmp_path), "status"])
    assert rc == 0
    assert captured["follow_pages"] is True
    assert captured["closed"] is True
