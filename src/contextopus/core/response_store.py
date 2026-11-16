"""Durable file-backed snapshot store for paginated responses and JIT delivery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any

from . import locking as fcntl
from .delivery import (
    CoreStructuredError,
    DeliveryManifest,
    DeliveryReceipt,
    DeliveryResult,
    dispatch_delivery_receipt,
)
from .response_pages import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_RESPONSE_CHARS,
    PAGE_FORMAT_VERSION,
    Snapshot,
    build_page,
    build_receipt,
    build_single_delivery_receipt,
    build_snapshot,
    canonical_json,
    fits_response,
    fits_with_transition_reservation,
    transition_fits_reservation,
    with_transition_first,
)
from .storage import StorageLayout, sync_directory

DEFAULT_TTL_SECONDS = 86400
_RESPONSE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PAGE_LOCKS_GUARD = threading.Lock()
_PAGE_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def _get_entry_rlock(store_dir: Path, response_id: str) -> threading.RLock:
    key = (str(store_dir), response_id)
    with _PAGE_LOCKS_GUARD:
        lock = _PAGE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PAGE_LOCKS[key] = lock
        return lock


def validate_response_id(response_id: Any) -> str:
    if not isinstance(response_id, str) or not _RESPONSE_ID_RE.fullmatch(response_id):
        raise CoreStructuredError(
            "invalid_arguments",
            "response_id must be a 32-character lowercase hexadecimal string",
            operation="core.page",
            execution_started=False,
        )
    return response_id


def _envelope_digest(payload: Mapping[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "integrity_sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _safe_unlink(path: Path) -> None:
    try:
        if path.is_symlink() or path.exists():
            path.unlink()
    except OSError:
        pass


class ResponseStore:
    """Thread- and process-safe disk cache at `<root>/runtime/response-pages`."""

    def __init__(
        self,
        layout: StorageLayout,
        *,
        root_id: str | None = None,
        delivery_dispatcher: Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.layout = layout
        self.root = layout.root
        self.root_id = root_id or str(self.root)
        self.runtime_dir = self.root / "runtime"
        self.store_dir = self.runtime_dir / "response-pages"
        self._delivery_dispatcher = delivery_dispatcher
        self._clock = clock
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.ttl_seconds = ttl_seconds

    def _entry_paths(self, response_id: str) -> tuple[Path, Path]:
        validate_response_id(response_id)
        return (
            self.store_dir / f"{response_id}.json",
            self.store_dir / f"{response_id}.lock",
        )

    def _check_directory_safety(self, *, error_code: str, operation: str, execution_started: bool) -> None:
        for directory in (self.root, self.runtime_dir, self.store_dir):
            if directory.is_symlink():
                raise CoreStructuredError(
                    error_code,
                    f"Refusing symlink directory in response cache path: {directory.name}",
                    operation=operation,
                    execution_started=execution_started,
                )
            if directory.exists() and not directory.is_dir():
                raise CoreStructuredError(
                    error_code,
                    f"Expected directory in response cache path: {directory.name}",
                    operation=operation,
                    execution_started=execution_started,
                )

    def _check_file_safety(
        self,
        response_id: str,
        *,
        error_code: str,
        operation: str,
        execution_started: bool,
    ) -> tuple[Path, Path]:
        json_path, lock_path = self._entry_paths(response_id)
        self._check_directory_safety(
            error_code=error_code,
            operation=operation,
            execution_started=execution_started,
        )
        for path in (json_path, lock_path):
            if path.is_symlink():
                raise CoreStructuredError(
                    error_code,
                    f"Refusing symlink file in response cache: {path.name}",
                    operation=operation,
                    execution_started=execution_started,
                )
        if self.store_dir.exists():
            resolved_dir = self.store_dir.resolve()
            for path in (json_path, lock_path):
                if path.exists() and path.resolve().parent != resolved_dir:
                    raise CoreStructuredError(
                        error_code,
                        f"Path escape detected in response cache: {path.name}",
                        operation=operation,
                        execution_started=execution_started,
                    )
        return json_path, lock_path

    def _ensure_store_dir(self, *, operation: str, execution_started: bool) -> None:
        self._check_directory_safety(
            error_code="response_cache_failed",
            operation=operation,
            execution_started=execution_started,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(exist_ok=True)
        self.store_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.store_dir, 0o700)

    @contextmanager
    def _entry_lock(
        self,
        response_id: str,
        *,
        create_dir: bool,
        error_code: str = "response_corrupt",
        operation: str = "core.page",
        execution_started: bool = False,
    ):
        validate_response_id(response_id)
        rlock = _get_entry_rlock(self.store_dir, response_id)
        rlock.acquire()
        try:
            json_path, lock_path = self._check_file_safety(
                response_id,
                error_code=error_code,
                operation=operation,
                execution_started=execution_started,
            )
            if create_dir:
                self._ensure_store_dir(operation=operation, execution_started=execution_started)
            if not self.store_dir.exists():
                yield json_path, lock_path
                return
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(lock_path, flags, 0o600)
            try:
                os.chmod(lock_path, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield json_path, lock_path
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            if not json_path.exists():
                _safe_unlink(lock_path)
        finally:
            rlock.release()

    def _atomic_write_record(
        self,
        response_id: str,
        json_path: Path,
        record: dict[str, Any],
    ) -> None:
        envelope = {k: v for k, v in record.items() if k != "integrity_sha256"}
        envelope["integrity_sha256"] = _envelope_digest(envelope)
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

        fd, tmp_name = tempfile.mkstemp(prefix=f".{response_id}.", suffix=".tmp", dir=self.store_dir)
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp_path, json_path)
            sync_directory(self.store_dir)
        except Exception:
            _safe_unlink(tmp_path)
            raise

    def _cleanup_expired_other_entries(self, *, exclude_response_id: str) -> None:
        if not self.store_dir.exists() or self.store_dir.is_symlink() or not self.store_dir.is_dir():
            return
        now = float(self._clock())
        try:
            entries = list(self.store_dir.iterdir())
        except OSError:
            return
        for path in entries:
            name = path.name
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            if stem == exclude_response_id or not _RESPONSE_ID_RE.fullmatch(stem):
                continue
            if path.is_symlink():
                continue
            lock_path = self.store_dir / f"{stem}.lock"
            if lock_path.is_symlink():
                continue
            rlock = _get_entry_rlock(self.store_dir, stem)
            if not rlock.acquire(blocking=False):
                continue
            try:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                except OSError:
                    continue
                unlinked_json = False
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        continue
                    try:
                        if not path.exists() or path.is_symlink():
                            continue
                        data = json.loads(path.read_text(encoding="utf-8"))
                        expires_at = float(data["expires_at"])
                        if now >= expires_at:
                            _safe_unlink(path)
                            _safe_unlink(lock_path)
                            unlinked_json = True
                    except (OSError, ValueError, TypeError, KeyError):
                        continue
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                if unlinked_json and not path.exists():
                    _safe_unlink(lock_path)
            finally:
                rlock.release()

    def save(
        self,
        snapshot: Snapshot,
        *,
        operation: str = "core.page",
        execution_started: bool = False,
    ) -> None:
        """Persist snapshot atomically under lock and run lazy cleanup of expired entries."""
        validate_response_id(snapshot.response_id)
        try:
            with self._entry_lock(
                snapshot.response_id,
                create_dir=True,
                error_code="response_cache_failed",
                operation=operation,
                execution_started=execution_started,
            ) as (json_path, _lock_path):
                self._cleanup_expired_other_entries(exclude_response_id=snapshot.response_id)
                created_at = float(self._clock())
                record = {
                    "format_version": snapshot.format_version,
                    "created_at": created_at,
                    "expires_at": created_at + self.ttl_seconds,
                    "root_id": self.root_id,
                    "context": snapshot.context,
                    "snapshot": snapshot.to_dict(),
                    "receipt_state": {
                        "issued_parts": [1],
                        "delivered_parts": [],
                    },
                }
                self._atomic_write_record(snapshot.response_id, json_path, record)
        except CoreStructuredError:
            raise
        except Exception as exc:
            raise CoreStructuredError(
                "response_cache_failed",
                f"Failed to persist paginated response {snapshot.response_id}: {exc}",
                operation=operation,
                execution_started=execution_started,
                response_id=snapshot.response_id,
                receipt={
                    "response_id": snapshot.response_id,
                    "part": 1,
                    "parts_total": snapshot.parts_total,
                    "result_revision": snapshot.result_revision,
                },
            ) from exc

    def _read_verified_record(
        self,
        response_id: str,
        json_path: Path,
        lock_path: Path,
        *,
        expected_context: str | None,
    ) -> tuple[dict[str, Any], Snapshot]:
        if not json_path.exists():
            raise CoreStructuredError(
                "response_not_found",
                f"No cached paginated response for response_id {response_id}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )
        try:
            raw_text = json_path.read_text(encoding="utf-8")
            record = json.loads(raw_text)
        except Exception as exc:
            raise CoreStructuredError(
                "response_corrupt",
                f"Corrupt paginated response cache entry {response_id}: {exc}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            ) from exc

        if not isinstance(record, dict):
            raise CoreStructuredError(
                "response_corrupt",
                f"Corrupt paginated response cache entry {response_id}: expected JSON object",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        fmt = record.get("format_version")
        if type(fmt) is not int:
            raise CoreStructuredError(
                "response_corrupt",
                f"Corrupt format_version in cached response {response_id}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )
        if fmt != PAGE_FORMAT_VERSION:
            raise CoreStructuredError(
                "response_format_unsupported",
                f"Unsupported paginated response format_version {fmt}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
                format_version=fmt,
            )

        stored_digest = record.get("integrity_sha256")
        if not isinstance(stored_digest, str) or stored_digest != _envelope_digest(record):
            raise CoreStructuredError(
                "response_corrupt",
                f"Integrity checksum mismatch for cached response {response_id}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        try:
            snapshot = Snapshot.from_dict(record["snapshot"])
            created_at = float(record["created_at"])
            expires_at = float(record["expires_at"])
        except Exception as exc:
            raise CoreStructuredError(
                "response_corrupt",
                f"Malformed snapshot payload for cached response {response_id}: {exc}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            ) from exc

        if snapshot.format_version != PAGE_FORMAT_VERSION:
            raise CoreStructuredError(
                "response_format_unsupported",
                f"Unsupported snapshot format_version {snapshot.format_version}",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        if record.get("root_id") != self.root_id or snapshot.root_id != self.root_id:
            raise CoreStructuredError(
                "response_root_mismatch",
                f"Cached response {response_id} belongs to another root",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        if record.get("context") != expected_context or snapshot.context != expected_context:
            raise CoreStructuredError(
                "response_context_mismatch",
                f"Cached response {response_id} does not match the active context",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        now = float(self._clock())
        if now < created_at:
            raise CoreStructuredError(
                "response_corrupt",
                f"Cached response {response_id} timestamp is in the future",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )
        if now >= expires_at:
            _safe_unlink(json_path)
            _safe_unlink(lock_path)
            raise CoreStructuredError(
                "response_expired",
                f"Cached response {response_id} expired after 24h TTL; re-run the original operation",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        return record, snapshot

    def mark_delivered(self, response_id: str, part: int) -> None:
        validate_response_id(response_id)
        if not self.store_dir.exists():
            return
        with self._entry_lock(response_id, create_dir=False) as (json_path, _lock_path):
            if not json_path.exists():
                return
            try:
                record = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(record, dict) or record.get("integrity_sha256") != _envelope_digest(record):
                return
            state = record.setdefault("receipt_state", {})
            issued = sorted({int(p) for p in state.get("issued_parts", [])} | {part})
            delivered = sorted({int(p) for p in state.get("delivered_parts", [])} | {part})
            state["issued_parts"] = issued
            state["delivered_parts"] = delivered
            self._atomic_write_record(response_id, json_path, record)

    def fetch_page(
        self,
        response_id: str,
        part: int,
        *,
        expected_context: str | None = None,
        delivery_handlers: Mapping[str, Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]]] | None = None,
        delivery_dispatcher: Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]] | None = None,
        skip_delivery: bool = False,
    ) -> dict[str, Any]:
        """Validate, retrieve and deliver a cached response page outside the cache lock."""
        validate_response_id(response_id)
        if type(part) is not int or part < 1:
            raise CoreStructuredError(
                "invalid_arguments",
                "part must be an integer >= 1",
                operation="core.page",
                execution_started=False,
                response_id=response_id,
            )

        with self._entry_lock(response_id, create_dir=False) as (json_path, lock_path):
            self._cleanup_expired_other_entries(exclude_response_id=response_id)
            _record, snapshot = self._read_verified_record(
                response_id,
                json_path,
                lock_path,
                expected_context=expected_context,
            )
            if part > snapshot.parts_total:
                raise CoreStructuredError(
                    "response_part_out_of_range",
                    f"Requested page {part} is out of range (1..{snapshot.parts_total})",
                    operation="core.page",
                    execution_started=False,
                    response_id=response_id,
                    part=part,
                    parts_total=snapshot.parts_total,
                )
            receipt = build_receipt(snapshot, part) if snapshot.delivery_owner else None

        # Delivery handler invocation happens STRICTLY outside _entry_lock.
        transition: dict[str, Any] | None = None
        if receipt is not None and not skip_delivery:
            dispatcher = delivery_dispatcher or self._delivery_dispatcher
            if dispatcher is not None:
                try:
                    delivery_res = DeliveryResult.from_value(dispatcher(receipt))
                except CoreStructuredError:
                    raise
                except Exception as exc:
                    from .delivery import DeliveryUnavailableError

                    raise DeliveryUnavailableError(
                        f"Delivery handler for {receipt.owner} failed: {exc}",
                        operation="core.page",
                        execution_started=False,
                    ) from exc
                if not delivery_res.accepted:
                    from .delivery import DeliveryUnavailableError

                    raise DeliveryUnavailableError(
                        delivery_res.error or f"Delivery receipt rejected by {receipt.owner}",
                        operation="core.page",
                        execution_started=False,
                    )
                transition = delivery_res.transition
            else:
                delivery_res = dispatch_delivery_receipt(
                    delivery_handlers or {},
                    receipt,
                    operation="core.page",
                    execution_started=False,
                )
                transition = delivery_res.transition

            if not transition_fits_reservation(transition):
                from .delivery import DeliveryUnavailableError

                raise DeliveryUnavailableError(
                    "Delivery transition exceeds 1024-byte JIT reservation",
                    operation="core.page",
                    execution_started=False,
                )
            self.mark_delivered(response_id, part)

        return build_page(snapshot, part, transition=transition)


def finalize_operation_delivery(
    result: Any,
    *,
    operation: str,
    kind: str,
    context: str | None,
    response_store: ResponseStore,
    delivery_handlers: Mapping[str, Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]]],
    delivery_manifest: DeliveryManifest | None = None,
    arguments: Mapping[str, Any] | None = None,
    detail: str = "compact",
    skip_presentation: bool = False,
    present_fn: Callable[..., Any] | None = None,
    delivery_dispatcher: Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]] | None = None,
) -> Any:
    """Apply presentation, size checks, pagination persistence and JIT delivery finalization."""
    if operation == "core.page":
        if not fits_response(result, max_bytes=response_store.max_bytes, max_chars=response_store.max_chars):
            raise CoreStructuredError(
                "response_corrupt",
                "core.page response exceeds configured size budget",
                operation="core.page",
                execution_started=False,
            )
        return result

    presented = result
    if not skip_presentation and present_fn is not None:
        presented = present_fn(result, operation=operation, arguments=arguments, detail=detail)

    reserve_transition = delivery_manifest is not None
    execution_started = kind == "execute"

    def _invoke_receipt(receipt: DeliveryReceipt) -> DeliveryResult:
        if delivery_dispatcher is not None:
            try:
                res = DeliveryResult.from_value(delivery_dispatcher(receipt))
            except CoreStructuredError:
                raise
            except Exception as exc:
                from .delivery import DeliveryUnavailableError

                raise DeliveryUnavailableError(
                    f"Delivery handler for {receipt.owner} failed: {exc}",
                    operation=operation,
                    execution_started=execution_started,
                ) from exc
            if not res.accepted:
                from .delivery import DeliveryUnavailableError

                raise DeliveryUnavailableError(
                    res.error or f"Delivery receipt rejected by {receipt.owner}",
                    operation=operation,
                    execution_started=execution_started,
                )
            return res
        return dispatch_delivery_receipt(
            delivery_handlers,
            receipt,
            operation=operation,
            execution_started=execution_started,
        )

    single_max_bytes = max(response_store.max_bytes, 24000) if operation == "core.context_read" else response_store.max_bytes
    single_max_chars = max(response_store.max_chars, 24000) if operation == "core.context_read" else response_store.max_chars

    if fits_with_transition_reservation(
        presented,
        reserve_transition=reserve_transition,
        max_bytes=single_max_bytes,
        max_chars=single_max_chars,
    ):
        if delivery_manifest is None:
            return presented
        receipt = build_single_delivery_receipt(
            presented,
            delivery_manifest,
            origin=operation,
            max_bytes=single_max_bytes,
            max_chars=single_max_chars,
        )
        delivery_res = _invoke_receipt(receipt)
        if not transition_fits_reservation(delivery_res.transition):
            from .delivery import DeliveryUnavailableError

            raise DeliveryUnavailableError(
                "Delivery transition exceeds 1024-byte JIT reservation",
                operation=operation,
                execution_started=execution_started,
            )
        if not isinstance(presented, dict):
            return presented
        final = with_transition_first(presented, delivery_res.transition)
        if not fits_response(final, max_bytes=single_max_bytes, max_chars=single_max_chars):
            raise CoreStructuredError(
                "delivery_unavailable",
                "Finalized response with transition exceeded response budget",
                operation=operation,
                execution_started=execution_started,
            )
        return final

    snapshot = build_snapshot(
        presented,
        origin=operation,
        context=context,
        root_id=response_store.root_id,
        delivery_manifest=delivery_manifest,
        max_bytes=response_store.max_bytes,
        max_chars=response_store.max_chars,
        reserve_transition=reserve_transition,
    )
    try:
        response_store.save(
            snapshot,
            operation=operation,
            execution_started=execution_started,
        )
    except CoreStructuredError as exc:
        receipt_stub = {
            "response_id": snapshot.response_id,
            "part": 1,
            "parts_total": snapshot.parts_total,
            "result_revision": snapshot.result_revision,
        }
        if exc.code == "response_cache_failed" and "receipt" in exc.payload.get("error", {}):
            raise
        raise CoreStructuredError(
            "response_cache_failed",
            str(exc),
            operation=operation,
            execution_started=execution_started,
            response_id=snapshot.response_id,
            receipt=receipt_stub,
        ) from exc

    transition: dict[str, Any] | None = None
    if delivery_manifest is not None:
        receipt = build_receipt(snapshot, 1)
        delivery_res = _invoke_receipt(receipt)
        if not transition_fits_reservation(delivery_res.transition):
            from .delivery import DeliveryUnavailableError

            raise DeliveryUnavailableError(
                "Delivery transition exceeds 1024-byte JIT reservation",
                operation=operation,
                execution_started=execution_started,
            )
        transition = delivery_res.transition
        response_store.mark_delivered(snapshot.response_id, 1)

    return build_page(snapshot, 1, transition=transition)
