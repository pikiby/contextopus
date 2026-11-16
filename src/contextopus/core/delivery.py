"""Internal delivery manifests, page receipts and module callback dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import re
import threading
from typing import Any

DELIVERY_MANIFEST_KEY = "__contextopus_delivery_manifest__"
_RESPONSE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_LOCAL = threading.local()


class CoreStructuredError(ValueError):
    """Domain/transport error with a stable machine-readable MCP error payload."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str = "core.page",
        execution_started: bool = False,
        recovery: str | None = None,
        **details: Any,
    ):
        self.code = code
        self.execution_started = execution_started
        error: dict[str, Any] = {
            "code": code,
            "message": message,
            "operation": operation,
            "execution_started": execution_started,
            **details,
        }
        if recovery is not None:
            error["recovery"] = recovery
        self.payload = {"status": "error", "error": error}
        super().__init__(message)


class DeliveryUnavailableError(CoreStructuredError):
    """Raised when the owning module cannot record or accept a delivery receipt."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "core.page",
        execution_started: bool = False,
        **details: Any,
    ):
        super().__init__(
            "delivery_unavailable",
            message,
            operation=operation,
            execution_started=execution_started,
            recovery="Ensure the owning module is enabled and inspect cycle/scope state before retrying.",
            **details,
        )


def _validate_pointer(pointer: str) -> str:
    if not isinstance(pointer, str) or (pointer != "" and not pointer.startswith("/")):
        raise ValueError(f"Invalid JSON Pointer payload path: {pointer!r}")
    return pointer


def _copy_json_dict(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return {str(k): deepcopy(v) for k, v in value.items()}


@dataclass(frozen=True)
class DeliverySubjectManifest:
    kind: str
    id: str
    revision: str | None
    payload_paths: tuple[str, ...]
    context: str | None = None
    eligible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("DeliverySubjectManifest.kind must be non-empty text")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("DeliverySubjectManifest.id must be non-empty text")
        if self.revision is not None and not isinstance(self.revision, str):
            raise ValueError("DeliverySubjectManifest.revision must be text or None")
        if self.context is not None and not isinstance(self.context, str):
            raise ValueError("DeliverySubjectManifest.context must be text or None")
        if not isinstance(self.eligible, bool):
            raise ValueError("DeliverySubjectManifest.eligible must be bool")
        paths = tuple(_validate_pointer(p) for p in self.payload_paths)
        if not paths:
            raise ValueError("DeliverySubjectManifest.payload_paths must be non-empty")
        object.__setattr__(self, "payload_paths", paths)
        object.__setattr__(self, "metadata", _copy_json_dict(self.metadata, "DeliverySubjectManifest.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "revision": self.revision,
            "payload_paths": list(self.payload_paths),
            "context": self.context,
            "eligible": self.eligible,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: DeliverySubjectManifest | Mapping[str, Any]) -> DeliverySubjectManifest:
        if isinstance(raw, DeliverySubjectManifest):
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("Delivery subject manifest must be an object")
        return cls(
            kind=raw["kind"],
            id=raw["id"],
            revision=raw.get("revision"),
            payload_paths=tuple(raw["payload_paths"]),
            context=raw.get("context"),
            eligible=raw.get("eligible", True),
            metadata=raw.get("metadata", {}),
        )


@dataclass(frozen=True)
class DeliveryManifest:
    owner: str
    context: str | None
    cycle_id: str | None = None
    scope_id: str | None = None
    operation: str | None = None
    subjects: tuple[DeliverySubjectManifest, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner:
            raise ValueError("DeliveryManifest.owner must be non-empty text")
        for attr in ("context", "cycle_id", "scope_id", "operation"):
            val = getattr(self, attr)
            if val is not None and not isinstance(val, str):
                raise ValueError(f"DeliveryManifest.{attr} must be text or None")
        subjects = tuple(DeliverySubjectManifest.from_dict(item) for item in self.subjects)
        object.__setattr__(self, "subjects", subjects)
        object.__setattr__(self, "metadata", _copy_json_dict(self.metadata, "DeliveryManifest.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "context": self.context,
            "cycle_id": self.cycle_id,
            "scope_id": self.scope_id,
            "operation": self.operation,
            "subjects": [item.to_dict() for item in self.subjects],
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: DeliveryManifest | Mapping[str, Any]) -> DeliveryManifest:
        if isinstance(raw, DeliveryManifest):
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("Delivery manifest must be an object")
        return cls(
            owner=raw["owner"],
            context=raw.get("context"),
            cycle_id=raw.get("cycle_id"),
            scope_id=raw.get("scope_id"),
            operation=raw.get("operation"),
            subjects=tuple(DeliverySubjectManifest.from_dict(s) for s in raw.get("subjects", ())),
            metadata=raw.get("metadata", {}),
        )


@dataclass(frozen=True)
class DeliverySubjectReceipt:
    kind: str
    id: str
    revision: str | None
    layout_revision: str
    parts_total: int
    issued_parts: tuple[int, ...]
    context: str | None = None
    eligible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.parts_total) is not int or self.parts_total < 1:
            raise ValueError("DeliverySubjectReceipt.parts_total must be >= 1")
        parts = tuple(sorted(set(self.issued_parts)))
        for part in parts:
            if type(part) is not int or not 1 <= part <= self.parts_total:
                raise ValueError("DeliverySubjectReceipt.issued_parts out of range")
        object.__setattr__(self, "issued_parts", parts)
        object.__setattr__(self, "metadata", _copy_json_dict(self.metadata, "DeliverySubjectReceipt.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "revision": self.revision,
            "layout_revision": self.layout_revision,
            "parts_total": self.parts_total,
            "issued_parts": list(self.issued_parts),
            "context": self.context,
            "eligible": self.eligible,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: DeliverySubjectReceipt | Mapping[str, Any]) -> DeliverySubjectReceipt:
        if isinstance(raw, DeliverySubjectReceipt):
            return raw
        return cls(
            kind=raw["kind"],
            id=raw["id"],
            revision=raw.get("revision"),
            layout_revision=raw["layout_revision"],
            parts_total=raw["parts_total"],
            issued_parts=tuple(raw.get("issued_parts", ())),
            context=raw.get("context"),
            eligible=raw.get("eligible", True),
            metadata=raw.get("metadata", {}),
        )


@dataclass(frozen=True)
class DeliveryReceipt:
    response_id: str
    part: int
    parts_total: int
    complete: bool
    owner: str
    context: str | None
    cycle_id: str | None = None
    scope_id: str | None = None
    operation: str | None = None
    subjects: tuple[DeliverySubjectReceipt, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.response_id, str) or not _RESPONSE_ID_RE.fullmatch(self.response_id):
            raise ValueError("DeliveryReceipt.response_id must be 32 lowercase hex chars")
        if type(self.parts_total) is not int or self.parts_total < 1:
            raise ValueError("DeliveryReceipt.parts_total must be >= 1")
        if type(self.part) is not int or not 1 <= self.part <= self.parts_total:
            raise ValueError("DeliveryReceipt.part out of range")
        object.__setattr__(
            self,
            "subjects",
            tuple(DeliverySubjectReceipt.from_dict(item) for item in self.subjects),
        )
        object.__setattr__(self, "metadata", _copy_json_dict(self.metadata, "DeliveryReceipt.metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "part": self.part,
            "parts_total": self.parts_total,
            "complete": self.complete,
            "owner": self.owner,
            "context": self.context,
            "cycle_id": self.cycle_id,
            "scope_id": self.scope_id,
            "operation": self.operation,
            "subjects": [item.to_dict() for item in self.subjects],
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: DeliveryReceipt | Mapping[str, Any]) -> DeliveryReceipt:
        if isinstance(raw, DeliveryReceipt):
            return raw
        return cls(
            response_id=raw["response_id"],
            part=raw["part"],
            parts_total=raw["parts_total"],
            complete=bool(raw["complete"]),
            owner=raw["owner"],
            context=raw.get("context"),
            cycle_id=raw.get("cycle_id"),
            scope_id=raw.get("scope_id"),
            operation=raw.get("operation"),
            subjects=tuple(DeliverySubjectReceipt.from_dict(s) for s in raw.get("subjects", ())),
            metadata=raw.get("metadata", {}),
        )


@dataclass(frozen=True)
class DeliveryResult:
    accepted: bool = True
    transition: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValueError("DeliveryResult.accepted must be bool")
        if self.transition is not None and not isinstance(self.transition, Mapping):
            raise ValueError("DeliveryResult.transition must be an object or None")
        if self.transition is not None:
            object.__setattr__(self, "transition", _copy_json_dict(self.transition, "DeliveryResult.transition"))
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("DeliveryResult.error must be text or None")
        object.__setattr__(self, "metadata", _copy_json_dict(self.metadata, "DeliveryResult.metadata"))

    @classmethod
    def from_value(cls, value: DeliveryResult | Mapping[str, Any]) -> DeliveryResult:
        if isinstance(value, DeliveryResult):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("Delivery handler must return DeliveryResult or a mapping")
        return cls(
            accepted=value.get("accepted", True),
            transition=value.get("transition"),
            error=value.get("error"),
            metadata=value.get("metadata", {}),
        )


def attach_delivery_manifest(
    result: dict[str, Any],
    manifest: DeliveryManifest | Mapping[str, Any],
) -> dict[str, Any]:
    """Attach a server-side delivery manifest for Core extraction prior to public output."""
    if not isinstance(result, dict):
        raise ValueError("Delivery manifest can only be attached to a dict result")
    normalized = DeliveryManifest.from_dict(manifest)
    return {**result, DELIVERY_MANIFEST_KEY: normalized}


def split_delivery_manifest(result: Any) -> tuple[Any, DeliveryManifest | None]:
    """Strip any internal delivery manifest from a result and normalize it."""
    if not isinstance(result, dict) or DELIVERY_MANIFEST_KEY not in result:
        return result, None
    raw = result[DELIVERY_MANIFEST_KEY]
    clean = {k: v for k, v in result.items() if k != DELIVERY_MANIFEST_KEY}
    if raw is None:
        return clean, None
    return clean, DeliveryManifest.from_dict(raw)


class _ManifestCapture:
    def __init__(self, target_depth: int):
        self.target_depth = target_depth
        self.manifest: DeliveryManifest | None = None


def current_invoke_depth() -> int:
    return getattr(_LOCAL, "invoke_depth", 0)


@contextmanager
def enter_invoke_scope():
    """Track OperationRegistry._invoke nesting depth per thread."""
    depth = current_invoke_depth() + 1
    _LOCAL.invoke_depth = depth
    try:
        yield depth
    finally:
        _LOCAL.invoke_depth = depth - 1


@contextmanager
def capture_delivery_manifest():
    """Capture the manifest from the outermost operation invoked inside this block."""
    stack = getattr(_LOCAL, "capture_stack", None)
    if stack is None:
        stack = []
        _LOCAL.capture_stack = stack
    slot = _ManifestCapture(current_invoke_depth() + 1)
    stack.append(slot)
    try:
        yield slot
    finally:
        stack.pop()


def record_captured_manifest(
    manifest: DeliveryManifest | None,
    *,
    owner: str | None = None,
) -> None:
    if manifest is None:
        return
    if owner is not None and manifest.owner != owner:
        raise ValueError(
            f"Delivery manifest owner {manifest.owner!r} does not match operation owner {owner!r}"
        )
    stack = getattr(_LOCAL, "capture_stack", None)
    if not stack:
        return
    slot = stack[-1]
    if current_invoke_depth() == slot.target_depth:
        slot.manifest = manifest


def dispatch_delivery_receipt(
    handlers: Mapping[str, Callable[[DeliveryReceipt], DeliveryResult | Mapping[str, Any]]],
    receipt: DeliveryReceipt,
    *,
    operation: str = "core.page",
    execution_started: bool = False,
) -> DeliveryResult:
    """Invoke the owning module's delivery handler outside any cache snapshot lock."""
    handler = handlers.get(receipt.owner)
    if handler is None:
        raise DeliveryUnavailableError(
            f"No delivery handler registered for module: {receipt.owner}",
            operation=operation,
            execution_started=execution_started,
        )
    try:
        raw = handler(receipt)
        result = DeliveryResult.from_value(raw)
    except DeliveryUnavailableError:
        raise
    except Exception as exc:
        raise DeliveryUnavailableError(
            f"Delivery handler for {receipt.owner} failed: {exc}",
            operation=operation,
            execution_started=execution_started,
        ) from exc
    if not result.accepted:
        raise DeliveryUnavailableError(
            result.error or f"Delivery receipt rejected by {receipt.owner}",
            operation=operation,
            execution_started=execution_started,
        )
    return result
