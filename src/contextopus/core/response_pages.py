"""Response size budgeting, deterministic pagination and lossless reconstruction."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
from typing import Any

from .delivery import (
    CoreStructuredError,
    DeliveryManifest,
    DeliveryReceipt,
    DeliverySubjectManifest,
    DeliverySubjectReceipt,
)

DEFAULT_MAX_RESPONSE_BYTES = 6000
DEFAULT_MAX_RESPONSE_CHARS = 6000
JIT_TRANSITION_RESERVATION_BYTES = 1024
INTERNAL_PART_MAX_BYTES = 2048
INTERNAL_PART_MAX_CHARS = 2048
BUDGET_COUNTER = "utf8_bytes"
PAGE_FORMAT_VERSION = 1

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r|$)")
_OPEN_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_H2_RE = re.compile(r"^[ ]{0,3}##(?:\s|$)")
_ATX_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}(?:\s|$)")
_BLANK_LINE_RE = re.compile(r"^[ \t]*(?:\r\n|\r|\n)?$")
_WORD_PIECE_RE = re.compile(r"\S+\s*|\s+")

_LARGE_SUMMARY_EXCLUDES = frozenset(
    {
        "content",
        "primary",
        "instructions",
        "documents",
        "docs",
        "project",
        "materials",
        "files",
        "sections",
        "scopes",
        "operations",
        "catalog",
        "changes",
        "relation_candidates",
        "review_changes",
        "diagnostics",
        "transition",
        "fragments",
        "page",
        "next_call",
    }
)


class _MissingType:
    pass


_MISSING = _MissingType()


def encoded_response_json(value: Any) -> str:
    """Serialize a JSON-compatible value using compact UTF-8 formatting."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def encoded_response_size(value: Any) -> tuple[int, int]:
    """Return (utf8_bytes, chars) of compact JSON serialization."""
    text = encoded_response_json(value)
    return len(text.encode("utf-8")), len(text)


def fits_response(
    value: Any,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
) -> bool:
    """Return True if value fits within both UTF-8 byte and char limits."""
    byte_len, char_len = encoded_response_size(value)
    return byte_len <= max_bytes and char_len <= max_chars


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_result_revision(value: Any) -> str:
    """Compute SHA-256 digest of a clean result excluding top-level JIT transition."""
    if isinstance(value, dict) and "transition" in value:
        value = {k: v for k, v in value.items() if k != "transition"}
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _transition_reservation_payload() -> dict[str, str]:
    # Adding this key under `"transition"` adds > 1024 UTF-8 bytes including key/colon overhead.
    return {"_jit_pad": "x" * JIT_TRANSITION_RESERVATION_BYTES}


def transition_fits_reservation(transition: Mapping[str, Any] | None) -> bool:
    if transition is None:
        return True
    byte_len, char_len = encoded_response_size(transition)
    return (
        byte_len <= JIT_TRANSITION_RESERVATION_BYTES
        and char_len <= JIT_TRANSITION_RESERVATION_BYTES
    )


def with_transition_first(
    payload: Mapping[str, Any],
    transition: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a dict with `transition` placed first when present."""
    clean = {k: v for k, v in payload.items() if k != "transition"}
    if transition is None:
        return clean
    return {"transition": deepcopy(dict(transition)), **clean}


def fits_with_transition_reservation(
    value: Any,
    *,
    reserve_transition: bool,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
) -> bool:
    if not fits_response(value, max_bytes=max_bytes, max_chars=max_chars):
        return False
    if not reserve_transition:
        return True
    if isinstance(value, Mapping):
        probe = {"transition": _transition_reservation_payload(), **{k: v for k, v in value.items() if k != "transition"}}
        return fits_response(probe, max_bytes=max_bytes, max_chars=max_chars)
    byte_len, char_len = encoded_response_size(value)
    overhead = JIT_TRANSITION_RESERVATION_BYTES + 24
    return (byte_len + overhead) <= max_bytes and (char_len + overhead) <= max_chars


def needs_pagination(
    value: Any,
    *,
    reserve_transition: bool = False,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
) -> bool:
    return not fits_with_transition_reservation(
        value,
        reserve_transition=reserve_transition,
        max_bytes=max_bytes,
        max_chars=max_chars,
    )


def escape_pointer_token(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def join_pointer(base: str, token: str | int) -> str:
    escaped = escape_pointer_token(str(token))
    return f"/{escaped}" if base == "" else f"{base}/{escaped}"


def split_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid RFC 6901 JSON Pointer: {pointer!r}")
    return [unescape_pointer_token(part) for part in pointer[1:].split("/")]


def resolve_pointer(root: Any, pointer: str) -> Any:
    current = root
    for segment in split_pointer(pointer):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if not segment.isdigit():
                return _MISSING
            idx = int(segment)
            if idx < 0 or idx >= len(current):
                return _MISSING
            current = current[idx]
        else:
            return _MISSING
    return current


def _split_lines_exact(text: str) -> list[str]:
    if text == "":
        return []
    return [part for part in _LINE_RE.findall(text) if part != ""]


def _is_closing_fence(line: str, fence_char: str, fence_len: int) -> bool:
    stripped = line.rstrip("\r\n")
    indent_stripped = stripped.lstrip(" ")
    if len(stripped) - len(indent_stripped) > 3:
        return False
    if not indent_stripped or indent_stripped[0] != fence_char:
        return False
    count = 0
    for ch in indent_stripped:
        if ch == fence_char:
            count += 1
        else:
            break
    if count < fence_len:
        return False
    return indent_stripped[count:].strip(" \t") == ""


def _analyze_markdown_boundaries(
    text: str,
) -> tuple[list[int], list[int], list[int], list[int], list[dict[str, Any]]]:
    """Return candidate split offsets by tier and per-line fence states."""
    lines = _split_lines_exact(text)
    h2_cuts: list[int] = []
    heading_cuts: list[int] = []
    blank_cuts: list[int] = []
    line_cuts: list[int] = []
    line_states: list[dict[str, Any]] = []

    offset = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_info = ""

    for idx, line in enumerate(lines):
        state_before = (
            {"in_fence": True, "fence_char": fence_char, "fence_length": fence_len, "info": fence_info}
            if in_fence
            else {"in_fence": False}
        )
        line_no_eol = line.rstrip("\r\n")
        if in_fence:
            closes = _is_closing_fence(line, fence_char, fence_len)
            state_after = (
                {"in_fence": False}
                if closes
                else {"in_fence": True, "fence_char": fence_char, "fence_length": fence_len, "info": fence_info}
            )
            if closes:
                in_fence = False
                fence_char = ""
                fence_len = 0
                fence_info = ""
        else:
            if offset > 0:
                if _H2_RE.match(line_no_eol):
                    h2_cuts.append(offset)
                    heading_cuts.append(offset)
                elif _ATX_HEADING_RE.match(line_no_eol):
                    heading_cuts.append(offset)
            m = _OPEN_FENCE_RE.match(line_no_eol)
            opened = False
            if m:
                marker, info = m.group(1), m.group(2)
                if marker[0] == "~" or "`" not in info:
                    opened = True
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                    fence_info = info.strip()
            state_after = (
                {"in_fence": True, "fence_char": fence_char, "fence_length": fence_len, "info": fence_info}
                if opened
                else {"in_fence": False}
            )

        end_offset = offset + len(line)
        line_states.append(
            {
                "start": offset,
                "end": end_offset,
                "before": state_before,
                "after": state_after,
            }
        )
        if end_offset < len(text):
            line_cuts.append(end_offset)
            if not state_after["in_fence"] and _BLANK_LINE_RE.match(line) and idx + 1 < len(lines):
                blank_cuts.append(end_offset)
        offset = end_offset

    return h2_cuts, heading_cuts, blank_cuts, line_cuts, line_states


def _longest_fitting_candidate(
    text: str,
    start: int,
    candidates: list[int],
    max_bytes: int,
    max_chars: int,
) -> int | None:
    if not candidates:
        return None
    left_idx = bisect_right(candidates, start)
    if left_idx >= len(candidates):
        return None
    sub = candidates[left_idx:]
    lo, hi = 0, len(sub) - 1
    best: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        cut = sub[mid]
        if fits_response(text[start:cut], max_bytes=max_bytes, max_chars=max_chars):
            best = cut
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _bisect_char_cut(text: str, start: int, max_bytes: int, max_chars: int) -> int:
    lo = start + 1
    hi = min(len(text), start + max_chars)
    best = start + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits_response(text[start:mid], max_bytes=max_bytes, max_chars=max_chars):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _word_cuts_in_slice(text: str, start: int, end: int) -> list[int]:
    cuts: list[int] = []
    pos = start
    for m in _WORD_PIECE_RE.finditer(text[start:end]):
        pos = start + m.end()
        if pos < end:
            cuts.append(pos)
    return cuts


def _fence_context_for_span(
    start: int,
    end: int,
    line_states: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not line_states:
        return None
    starts = [entry["start"] for entry in line_states]
    first_idx = max(0, bisect_right(starts, start) - 1)
    last_idx = max(0, bisect_right(starts, max(start, end - 1)) - 1)
    first = line_states[first_idx]
    last = line_states[last_idx]
    start_state = first["before"] if start == first["start"] else first["after"]
    end_state = last["after"] if end == last["end"] else last["after"]
    if not start_state["in_fence"] and not end_state["in_fence"]:
        return None
    ref = start_state if start_state["in_fence"] else end_state
    return {
        "starts_in_fence": bool(start_state["in_fence"]),
        "ends_in_fence": bool(end_state["in_fence"]),
        "fence_char": ref["fence_char"],
        "fence_length": ref["fence_length"],
        "info": ref["info"],
    }


def split_markdown_chunks(
    text: str,
    *,
    max_bytes: int,
    max_chars: int,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Split markdown text preserving exact characters using 5-tier boundary preference."""
    if text == "" or fits_response(text, max_bytes=max_bytes, max_chars=max_chars):
        return [(text, None)]
    if max_bytes < 16 or max_chars < 16:
        raise ValueError("Markdown chunk budget is too small to fit a single JSON string")

    h2_cuts, heading_cuts, blank_cuts, line_cuts, line_states = _analyze_markdown_boundaries(text)
    chunks: list[tuple[str, dict[str, Any] | None]] = []
    pos = 0
    total = len(text)

    while pos < total:
        if fits_response(text[pos:], max_bytes=max_bytes, max_chars=max_chars):
            cut = total
        else:
            cut = _longest_fitting_candidate(text, pos, h2_cuts, max_bytes, max_chars)
            if cut is None:
                cut = _longest_fitting_candidate(text, pos, heading_cuts, max_bytes, max_chars)
            if cut is None:
                cut = _longest_fitting_candidate(text, pos, blank_cuts, max_bytes, max_chars)
            if cut is None:
                cut = _longest_fitting_candidate(text, pos, line_cuts, max_bytes, max_chars)
            if cut is None:
                next_line_idx = bisect_right(line_cuts, pos)
                line_end = line_cuts[next_line_idx] if next_line_idx < len(line_cuts) else total
                wcuts = _word_cuts_in_slice(text, pos, line_end)
                cut = _longest_fitting_candidate(text, pos, wcuts, max_bytes, max_chars)
            if cut is None:
                cut = _bisect_char_cut(text, pos, max_bytes, max_chars)
                if not fits_response(text[pos:cut], max_bytes=max_bytes, max_chars=max_chars):
                    raise ValueError("Unable to split string within response budget")
        fence_ctx = _fence_context_for_span(pos, cut, line_states)
        chunks.append((text[pos:cut], fence_ctx))
        pos = cut
    return chunks


@dataclass(frozen=True)
class SubjectLayout:
    kind: str
    id: str
    revision: str | None
    layout_revision: str
    parts_total: int
    page_parts: dict[int, tuple[int, ...]] = field(default_factory=dict)
    context: str | None = None
    eligible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "revision": self.revision,
            "layout_revision": self.layout_revision,
            "parts_total": self.parts_total,
            "page_parts": {str(k): list(v) for k, v in sorted(self.page_parts.items())},
            "context": self.context,
            "eligible": self.eligible,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SubjectLayout:
        raw_pages = raw.get("page_parts", {})
        return cls(
            kind=str(raw["kind"]),
            id=str(raw["id"]),
            revision=raw.get("revision"),
            layout_revision=str(raw["layout_revision"]),
            parts_total=int(raw["parts_total"]),
            page_parts={int(k): tuple(int(p) for p in v) for k, v in raw_pages.items()},
            context=raw.get("context"),
            eligible=bool(raw.get("eligible", True)),
            metadata=deepcopy(dict(raw.get("metadata", {}))),
        )


@dataclass(frozen=True)
class Snapshot:
    format_version: int
    response_id: str
    origin: str
    context: str | None
    root_id: str
    result_revision: str
    max_bytes: int
    max_chars: int
    reserve_transition: bool
    summary: dict[str, Any]
    pages: tuple[list[dict[str, Any]], ...]
    delivery_owner: str | None = None
    cycle_id: str | None = None
    scope_id: str | None = None
    delivery_operation: str | None = None
    delivery_metadata: dict[str, Any] = field(default_factory=dict)
    subjects: tuple[SubjectLayout, ...] = ()

    @property
    def parts_total(self) -> int:
        return len(self.pages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "response_id": self.response_id,
            "origin": self.origin,
            "context": self.context,
            "root_id": self.root_id,
            "result_revision": self.result_revision,
            "max_bytes": self.max_bytes,
            "max_chars": self.max_chars,
            "reserve_transition": self.reserve_transition,
            "summary": deepcopy(self.summary),
            "pages": [deepcopy(page) for page in self.pages],
            "delivery_owner": self.delivery_owner,
            "cycle_id": self.cycle_id,
            "scope_id": self.scope_id,
            "delivery_operation": self.delivery_operation,
            "delivery_metadata": deepcopy(self.delivery_metadata),
            "subjects": [s.to_dict() for s in self.subjects],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Snapshot:
        return cls(
            format_version=int(raw["format_version"]),
            response_id=str(raw["response_id"]),
            origin=str(raw["origin"]),
            context=raw.get("context"),
            root_id=str(raw["root_id"]),
            result_revision=str(raw["result_revision"]),
            max_bytes=int(raw["max_bytes"]),
            max_chars=int(raw["max_chars"]),
            reserve_transition=bool(raw.get("reserve_transition", False)),
            summary=deepcopy(dict(raw.get("summary", {}))),
            pages=tuple(deepcopy(list(p)) for p in raw["pages"]),
            delivery_owner=raw.get("delivery_owner"),
            cycle_id=raw.get("cycle_id"),
            scope_id=raw.get("scope_id"),
            delivery_operation=raw.get("delivery_operation"),
            delivery_metadata=deepcopy(dict(raw.get("delivery_metadata", {}))),
            subjects=tuple(SubjectLayout.from_dict(s) for s in raw.get("subjects", ())),
        )


@dataclass
class _PlannedFragment:
    fragment: dict[str, Any]
    subject_tag: tuple[int, int] | None = None  # (subject_index, 1-based internal part)


def _max_fence_info_sample(text: str) -> tuple[str, int]:
    heaviest_info = ""
    heaviest_bytes = encoded_response_size("")[0]
    max_fence_len = 10
    for line in _split_lines_exact(text):
        m = _OPEN_FENCE_RE.match(line.rstrip("\r\n"))
        if m:
            marker, info = m.group(1), m.group(2).strip()
            max_fence_len = max(max_fence_len, len(marker))
            info_bytes = encoded_response_size(info)[0]
            if info_bytes > heaviest_bytes:
                heaviest_bytes = info_bytes
                heaviest_info = info
    return heaviest_info, max_fence_len


def _text_wrapper_overhead(path: str, sample_info: str = "", *, fence_length: int = 10) -> tuple[int, int]:
    probe_fence = {
        "path": path,
        "kind": "text",
        "part": 99999,
        "parts": 99999,
        "text": "",
        "fence_context": {
            "starts_in_fence": True,
            "ends_in_fence": True,
            "fence_char": "`",
            "fence_length": max(10, fence_length),
            "info": sample_info,
        },
    }
    wb, wc = encoded_response_size(probe_fence)
    vb, vc = encoded_response_size({"path": path, "kind": "value", "value": ""})
    # Empty JSON string `""` contributes 2 ASCII bytes/chars; subtract them so caller compares encoded_response_size(chunk).
    return max(wb, vb) - 2, max(wc, vc) - 2


def _effective_internal_limits(
    frag_budget_bytes: int,
    frag_budget_chars: int,
    *,
    max_wrapper_bytes: int = 160,
    max_wrapper_chars: int = 160,
) -> tuple[int, int]:
    avail_bytes = frag_budget_bytes - max_wrapper_bytes
    avail_chars = frag_budget_chars - max_wrapper_chars
    if avail_bytes < 24 or avail_chars < 24:
        raise CoreStructuredError(
            "invalid_arguments",
            "Response page budget is too small for fragment wrapper overhead",
            operation="core.page",
            frag_budget_bytes=frag_budget_bytes,
            frag_budget_chars=frag_budget_chars,
        )
    return min(INTERNAL_PART_MAX_BYTES, avail_bytes), min(INTERNAL_PART_MAX_CHARS, avail_chars)


def _compute_subject_internal_limits(
    clean_result: Any,
    delivery_manifest: DeliveryManifest | None,
    *,
    frag_budget_bytes: int,
    frag_budget_chars: int,
) -> tuple[int, int]:
    max_subj_over_b, max_subj_over_c = 160, 160
    if delivery_manifest is not None:
        for subject in delivery_manifest.subjects:
            for ptr in subject.payload_paths:
                val = resolve_pointer(clean_result, ptr)
                info, fence_len = _max_fence_info_sample(val) if isinstance(val, str) else ("", 10)
                ob, oc = _text_wrapper_overhead(ptr, info, fence_length=fence_len)
                max_subj_over_b = max(max_subj_over_b, ob)
                max_subj_over_c = max(max_subj_over_c, oc)
    return _effective_internal_limits(
        frag_budget_bytes,
        frag_budget_chars,
        max_wrapper_bytes=max_subj_over_b,
        max_wrapper_chars=max_subj_over_c,
    )


def _split_non_string_internal_parts(value: Any, max_bytes: int, max_chars: int) -> list[Any]:
    if fits_response(value, max_bytes=max_bytes, max_chars=max_chars):
        return [deepcopy(value)]
    if isinstance(value, Mapping):
        parts: list[Any] = []
        current: dict[str, Any] = {}
        for key, item in value.items():
            skey = str(key)
            candidate = {**current, skey: item}
            if fits_response(candidate, max_bytes=max_bytes, max_chars=max_chars):
                current[skey] = deepcopy(item)
                continue
            if current:
                parts.append(current)
                current = {}
            single = {skey: item}
            if fits_response(single, max_bytes=max_bytes, max_chars=max_chars):
                current = deepcopy(single)
            elif isinstance(item, str):
                for text_chunk, _ in split_markdown_chunks(item, max_bytes=max(64, max_bytes - len(skey) - 16), max_chars=max(64, max_chars - len(skey) - 16)):
                    parts.append({skey: text_chunk})
            else:
                for sub in _split_non_string_internal_parts(item, max(64, max_bytes - len(skey) - 16), max(64, max_chars - len(skey) - 16)):
                    parts.append({skey: sub})
        if current:
            parts.append(current)
        return parts or [{}]
    if isinstance(value, list):
        parts = []
        batch: list[Any] = []
        for item in value:
            candidate = [*batch, item]
            if fits_response(candidate, max_bytes=max_bytes, max_chars=max_chars):
                batch.append(deepcopy(item))
                continue
            if batch:
                parts.append(batch)
                batch = []
            if fits_response([item], max_bytes=max_bytes, max_chars=max_chars):
                batch = [deepcopy(item)]
            elif isinstance(item, str):
                for text_chunk, _ in split_markdown_chunks(item, max_bytes=max(64, max_bytes - 16), max_chars=max(64, max_chars - 16)):
                    parts.append([text_chunk])
            else:
                parts.extend(_split_non_string_internal_parts(item, max_bytes, max_chars))
        if batch:
            parts.append(batch)
        return parts or [[]]
    return [deepcopy(value)]


def _compute_subject_internal_parts(
    clean_result: Any,
    subject: DeliverySubjectManifest,
    *,
    internal_max_bytes: int,
    internal_max_chars: int,
) -> tuple[str, int, dict[str, list[tuple[int, str, dict[str, Any] | None]]]]:
    """Compute deterministic layout_revision, parts_total and per-pointer string chunks."""
    canonical_parts: list[Any] = []
    pointer_chunks: dict[str, list[tuple[int, str, dict[str, Any] | None]]] = {}
    multi_path = len(subject.payload_paths) > 1
    next_part_num = 1
    any_found = False

    for idx, pointer in enumerate(subject.payload_paths):
        value = resolve_pointer(clean_result, pointer)
        if value is _MISSING:
            canonical_parts.append({"slot": idx, "missing": True} if multi_path else None)
            continue
        any_found = True
        if isinstance(value, str):
            raw_chunks = split_markdown_chunks(
                value,
                max_bytes=internal_max_bytes,
                max_chars=internal_max_chars,
            )
            assigned: list[tuple[int, str, dict[str, Any] | None]] = []
            for text_chunk, fence_ctx in raw_chunks:
                part_no = next_part_num
                next_part_num += 1
                assigned.append((part_no, text_chunk, fence_ctx))
                canonical_parts.append({"slot": idx, "v": text_chunk} if multi_path else text_chunk)
            pointer_chunks[pointer] = assigned
        else:
            for chunk in _split_non_string_internal_parts(value, internal_max_bytes, internal_max_chars):
                part_no = next_part_num
                next_part_num += 1
                canonical_parts.append({"slot": idx, "v": chunk} if multi_path else chunk)

    parts_total = max(1, (next_part_num - 1) if any_found else 1)
    layout_revision = hashlib.sha256(
        canonical_json({"v": 1, "parts": canonical_parts}).encode("utf-8")
    ).hexdigest()
    return layout_revision, parts_total, pointer_chunks


_PRIORITY_SUMMARY_KEYS = (
    "cycle_id",
    "status",
    "purpose",
    "replayed",
    "task_revision",
    "clarification_count",
)


def extract_summary(value: Any) -> dict[str, Any]:
    """Extract small top-level scalar metadata prioritizing cycle/task fields."""
    if not isinstance(value, Mapping):
        return {"value_type": type(value).__name__}
    summary: dict[str, Any] = {}
    ordered_keys = [
        *[k for k in _PRIORITY_SUMMARY_KEYS if k in value],
        *[str(k) for k in value.keys() if str(k) not in _PRIORITY_SUMMARY_KEYS],
    ]
    for skey in ordered_keys:
        if skey in _LARGE_SUMMARY_EXCLUDES or skey not in value:
            continue
        item = value[skey]
        if isinstance(item, (str, int, float, bool)) or item is None:
            if isinstance(item, str) and encoded_response_size(item)[0] > 256:
                continue
            probe = {**summary, skey: item}
            if encoded_response_size(probe)[0] <= 512:
                summary[skey] = item
    return summary


def _doc_identifier(doc: Mapping[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    for field_name in ("id", "document_id", "path", "title"):
        val = doc.get(field_name)
        if isinstance(val, str) and val:
            keys.append(val)
    return tuple(keys)


def _doc_dependencies(doc: Mapping[str, Any]) -> list[str]:
    deps: list[str] = []
    raw_top = doc.get("depends_on")
    if isinstance(raw_top, list):
        deps.extend(str(item) for item in raw_top if isinstance(item, str) and item)
    meta = doc.get("metadata")
    if isinstance(meta, Mapping):
        raw_meta = meta.get("depends_on")
        if isinstance(raw_meta, list):
            deps.extend(str(item) for item in raw_meta if isinstance(item, str) and item)
    return deps


def _topological_doc_indices(docs: list[Any]) -> list[int]:
    """Order doc list indices by depends_on dependencies while preserving stable tie-breaks."""
    n = len(docs)
    id_to_index: dict[str, int] = {}
    for idx, item in enumerate(docs):
        if isinstance(item, Mapping):
            for ident in _doc_identifier(item):
                id_to_index.setdefault(ident, idx)

    prereqs: list[set[int]] = [set() for _ in range(n)]
    dependents: list[list[int]] = [[] for _ in range(n)]
    for idx, item in enumerate(docs):
        if not isinstance(item, Mapping):
            continue
        for dep in _doc_dependencies(item):
            target = id_to_index.get(dep)
            if target is not None and target != idx and target not in prereqs[idx]:
                prereqs[idx].add(target)
                dependents[target].append(idx)

    indegree = [len(p) for p in prereqs]
    ready = sorted(i for i in range(n) if indegree[i] == 0)
    order: list[int] = []
    seen: set[int] = set()
    while ready:
        cur = ready.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        for nxt in dependents[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0 and nxt not in seen:
                ready.append(nxt)
        ready.sort()

    for idx in range(n):
        if idx not in seen:
            order.append(idx)
    return order


def _plan_text_fragments(
    path: str,
    text: str,
    *,
    max_chunk_bytes: int,
    max_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
) -> list[_PlannedFragment]:
    if path in subject_pointer_map:
        subject_idx, assigned = subject_pointer_map[path]
        total = len(assigned)
        planned: list[_PlannedFragment] = []
        for part_idx, (internal_part_no, chunk_text, fence_ctx) in enumerate(assigned, start=1):
            frag: dict[str, Any] = {
                "path": path,
                "kind": "text",
                "part": part_idx,
                "parts": total,
                "text": chunk_text,
            }
            if fence_ctx is not None:
                frag["fence_context"] = fence_ctx
            planned.append(_PlannedFragment(fragment=frag, subject_tag=(subject_idx, internal_part_no)))
        return planned

    sample_info, max_fence_len = _max_fence_info_sample(text)
    over_b, over_c = _text_wrapper_overhead(path, sample_info, fence_length=max_fence_len)
    allowed_bytes = max_chunk_bytes - over_b
    allowed_chars = max_chunk_chars - over_c
    if allowed_bytes < 24 or allowed_chars < 24:
        raise CoreStructuredError(
            "invalid_arguments",
            "Response page budget is too small to fit text fragment wrapper",
            operation="core.page",
            fragment_path=path,
        )
    chunks = split_markdown_chunks(text, max_bytes=allowed_bytes, max_chars=allowed_chars)
    total = len(chunks)
    if total == 1 and fits_response({"path": path, "kind": "value", "value": text}, max_bytes=max_chunk_bytes, max_chars=max_chunk_chars):
        return [_PlannedFragment(fragment={"path": path, "kind": "value", "value": text})]
    planned = []
    for part_idx, (chunk_text, fence_ctx) in enumerate(chunks, start=1):
        frag = {
            "path": path,
            "kind": "text",
            "part": part_idx,
            "parts": total,
            "text": chunk_text,
        }
        if fence_ctx is not None:
            frag["fence_context"] = fence_ctx
        planned.append(_PlannedFragment(fragment=frag))
    return planned


def _has_subject_prefix(path: str, subject_pointer_map: dict[str, Any]) -> bool:
    if path in subject_pointer_map:
        return True
    prefix = "/" if path == "" else f"{path}/"
    return any(p.startswith(prefix) for p in subject_pointer_map)


def _plan_value_fragments(
    path: str,
    value: Any,
    *,
    page_frag_budget_bytes: int,
    page_frag_budget_chars: int,
    non_subject_chunk_bytes: int,
    non_subject_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
    subject_non_string_map: dict[str, int],
) -> list[_PlannedFragment]:
    if isinstance(value, str):
        return _plan_text_fragments(
            path,
            value,
            max_chunk_bytes=non_subject_chunk_bytes,
            max_chunk_chars=non_subject_chunk_chars,
            subject_pointer_map=subject_pointer_map,
        )

    if path in subject_non_string_map and not isinstance(value, str):
        subj_idx = subject_non_string_map[path]
        single = {"path": path, "kind": "value", "value": deepcopy(value)}
        if fits_response(single, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
            return [_PlannedFragment(fragment=single, subject_tag=(subj_idx, 1))]

    # Keep small non-container or empty values as one `"value"` fragment unless a descendant owns a subject chunk.
    if not isinstance(value, (dict, list)) or (
        not value
        and path not in subject_non_string_map
    ):
        frag = {"path": path, "kind": "value", "value": deepcopy(value)}
        if not fits_response(frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
            raise CoreStructuredError(
                "invalid_arguments",
                "Single leaf fragment exceeds page response budget",
                operation="core.page",
                fragment_path=path,
            )
        tag = (subject_non_string_map[path], 1) if path in subject_non_string_map else None
        return [_PlannedFragment(fragment=frag, subject_tag=tag)]

    has_descendant_subject = _has_subject_prefix(path, subject_pointer_map) or _has_subject_prefix(path, subject_non_string_map)
    if not has_descendant_subject:
        single_frag = {"path": path, "kind": "value", "value": deepcopy(value)}
        if fits_response(single_frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
            return [_PlannedFragment(fragment=single_frag)]

    if isinstance(value, dict):
        return _plan_dict_fragments(
            path,
            value,
            page_frag_budget_bytes=page_frag_budget_bytes,
            page_frag_budget_chars=page_frag_budget_chars,
            non_subject_chunk_bytes=non_subject_chunk_bytes,
            non_subject_chunk_chars=non_subject_chunk_chars,
            subject_pointer_map=subject_pointer_map,
            subject_non_string_map=subject_non_string_map,
        )

    return _plan_list_fragments(
        path,
        value,
        page_frag_budget_bytes=page_frag_budget_bytes,
        page_frag_budget_chars=page_frag_budget_chars,
        non_subject_chunk_bytes=non_subject_chunk_bytes,
        non_subject_chunk_chars=non_subject_chunk_chars,
        subject_pointer_map=subject_pointer_map,
        subject_non_string_map=subject_non_string_map,
    )


def _plan_dict_fragments(
    path: str,
    mapping: Mapping[str, Any],
    *,
    page_frag_budget_bytes: int,
    page_frag_budget_chars: int,
    non_subject_chunk_bytes: int,
    non_subject_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
    subject_non_string_map: dict[str, int],
    key_order: Sequence[str] | None = None,
) -> list[_PlannedFragment]:
    ordered_keys = list(key_order) if key_order is not None else [str(k) for k in mapping.keys()]
    planned: list[_PlannedFragment] = []
    batch_keys: list[str] = []
    batch_values: dict[str, Any] = {}

    def flush_batch() -> None:
        nonlocal batch_keys, batch_values
        if not batch_keys:
            return
        planned.append(
            _PlannedFragment(
                fragment={
                    "path": path,
                    "kind": "object",
                    "keys": list(batch_keys),
                    "value": deepcopy(batch_values),
                }
            )
        )
        batch_keys = []
        batch_values = {}

    for key in ordered_keys:
        if key not in mapping:
            continue
        item = mapping[key]
        child_path = join_pointer(path, key)
        child_has_subject = _has_subject_prefix(child_path, subject_pointer_map) or _has_subject_prefix(child_path, subject_non_string_map)

        if not child_has_subject:
            cand_keys = [*batch_keys, key]
            cand_values = {**batch_values, key: item}
            cand_frag = {"path": path, "kind": "object", "keys": cand_keys, "value": cand_values}
            if fits_response(cand_frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
                batch_keys.append(key)
                batch_values[key] = deepcopy(item)
                continue
            if batch_keys:
                flush_batch()
                single_frag = {"path": path, "kind": "object", "keys": [key], "value": {key: item}}
                if fits_response(single_frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
                    batch_keys = [key]
                    batch_values = {key: deepcopy(item)}
                    continue
        else:
            flush_batch()

        planned.extend(
            _plan_value_fragments(
                child_path,
                item,
                page_frag_budget_bytes=page_frag_budget_bytes,
                page_frag_budget_chars=page_frag_budget_chars,
                non_subject_chunk_bytes=non_subject_chunk_bytes,
                non_subject_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
                subject_non_string_map=subject_non_string_map,
            )
        )

    flush_batch()
    return planned


def _plan_list_fragments(
    path: str,
    items: Sequence[Any],
    *,
    page_frag_budget_bytes: int,
    page_frag_budget_chars: int,
    non_subject_chunk_bytes: int,
    non_subject_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
    subject_non_string_map: dict[str, int],
) -> list[_PlannedFragment]:
    total_len = len(items)
    planned: list[_PlannedFragment] = []
    batch_start: int | None = None
    batch_items: list[Any] = []
    emitted_anchor = False

    def flush_batch() -> None:
        nonlocal batch_start, batch_items, emitted_anchor
        if batch_start is None or not batch_items:
            return
        planned.append(
            _PlannedFragment(
                fragment={
                    "path": path,
                    "kind": "array",
                    "start": batch_start,
                    "length": total_len,
                    "items": deepcopy(batch_items),
                }
            )
        )
        emitted_anchor = True
        batch_start = None
        batch_items = []

    for idx, item in enumerate(items):
        child_path = join_pointer(path, idx)
        child_has_subject = _has_subject_prefix(child_path, subject_pointer_map) or _has_subject_prefix(child_path, subject_non_string_map)

        if not child_has_subject:
            cand_start = idx if batch_start is None else batch_start
            cand_items = [*batch_items, item]
            cand_frag = {
                "path": path,
                "kind": "array",
                "start": cand_start,
                "length": total_len,
                "items": cand_items,
            }
            if fits_response(cand_frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
                if batch_start is None:
                    batch_start = idx
                batch_items.append(deepcopy(item))
                continue
            if batch_items:
                flush_batch()
                single_frag = {
                    "path": path,
                    "kind": "array",
                    "start": idx,
                    "length": total_len,
                    "items": [item],
                }
                if fits_response(single_frag, max_bytes=page_frag_budget_bytes, max_chars=page_frag_budget_chars):
                    batch_start = idx
                    batch_items = [deepcopy(item)]
                    continue
        else:
            flush_batch()

        if not emitted_anchor:
            planned.append(
                _PlannedFragment(
                    fragment={
                        "path": path,
                        "kind": "array",
                        "start": 0,
                        "length": total_len,
                        "items": [],
                    }
                )
            )
            emitted_anchor = True

        planned.extend(
            _plan_value_fragments(
                child_path,
                item,
                page_frag_budget_bytes=page_frag_budget_bytes,
                page_frag_budget_chars=page_frag_budget_chars,
                non_subject_chunk_bytes=non_subject_chunk_bytes,
                non_subject_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
                subject_non_string_map=subject_non_string_map,
            )
        )

    flush_batch()
    if not emitted_anchor:
        planned.append(
            _PlannedFragment(
                fragment={
                    "path": path,
                    "kind": "array",
                    "start": 0,
                    "length": total_len,
                    "items": [],
                }
            )
        )
    return planned


def _detect_doc_list_location(mapping: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ('top' | 'instructions', key) for a list of instruction document dicts."""
    instr = mapping.get("instructions")
    if isinstance(instr, Mapping):
        for candidate in ("documents", "docs"):
            val = instr.get(candidate)
            if isinstance(val, list) and all(isinstance(d, Mapping) for d in val):
                return ("instructions", candidate)
    for candidate in ("instructions", "documents", "docs"):
        val = mapping.get(candidate)
        if isinstance(val, list) and val and all(isinstance(d, Mapping) for d in val):
            return ("top", candidate)
    return None


def _is_startup_payload(origin: str, value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if origin == "startup" or origin.endswith(".startup"):
        return True
    instr = value.get("instructions")
    has_nested_startup = isinstance(instr, Mapping) and (
        isinstance(instr.get("content"), str) or _detect_doc_list_location(value) is not None
    )
    has_primary = isinstance(value.get("primary"), Mapping)
    has_docs = _detect_doc_list_location(value) is not None
    has_project = isinstance(value.get("project"), Mapping)
    return has_nested_startup or (has_primary and (has_docs or has_project))


def _emit_docs_toc_and_bodies(
    docs_ptr: str,
    docs: list[Any],
    *,
    page_frag_budget_bytes: int,
    page_frag_budget_chars: int,
    non_subject_chunk_bytes: int,
    non_subject_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
    subject_non_string_map: dict[str, int],
) -> list[_PlannedFragment]:
    planned: list[_PlannedFragment] = [
        _PlannedFragment(
            fragment={
                "path": docs_ptr,
                "kind": "array",
                "start": 0,
                "length": len(docs),
                "items": [],
            }
        )
    ]
    deferred_bodies: list[tuple[int, str, str]] = []
    ordered_indices = _topological_doc_indices(docs)
    for idx, doc in enumerate(docs):
        doc_ptr = join_pointer(docs_ptr, idx)
        if isinstance(doc, Mapping) and isinstance(doc.get("content"), str):
            toc_keys = [str(k) for k in doc.keys() if str(k) != "content"]
            if toc_keys:
                planned.extend(
                    _plan_dict_fragments(
                        doc_ptr,
                        doc,
                        page_frag_budget_bytes=page_frag_budget_bytes,
                        page_frag_budget_chars=page_frag_budget_chars,
                        non_subject_chunk_bytes=non_subject_chunk_bytes,
                        non_subject_chunk_chars=non_subject_chunk_chars,
                        subject_pointer_map=subject_pointer_map,
                        subject_non_string_map=subject_non_string_map,
                        key_order=toc_keys,
                    )
                )
            deferred_bodies.append((idx, join_pointer(doc_ptr, "content"), doc["content"]))
        else:
            planned.extend(
                _plan_value_fragments(
                    doc_ptr,
                    doc,
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                )
            )
    body_map = {idx: (cptr, ctext) for idx, cptr, ctext in deferred_bodies}
    for idx in ordered_indices:
        if idx not in body_map:
            continue
        cptr, ctext = body_map[idx]
        planned.extend(
            _plan_text_fragments(
                cptr,
                ctext,
                max_chunk_bytes=non_subject_chunk_bytes,
                max_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
            )
        )
    return planned


def _plan_startup_fragments(
    mapping: Mapping[str, Any],
    *,
    page_frag_budget_bytes: int,
    page_frag_budget_chars: int,
    non_subject_chunk_bytes: int,
    non_subject_chunk_chars: int,
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]],
    subject_non_string_map: dict[str, int],
) -> list[_PlannedFragment]:
    """Plan fragments in order: summary -> primary -> docs TOC -> doc bodies (dependency order) -> project -> rest."""
    planned: list[_PlannedFragment] = []
    doc_loc = _detect_doc_list_location(mapping)
    handled: set[str] = set()

    scalar_keys: list[str] = []
    special = {"primary", "project"}
    if isinstance(mapping.get("instructions"), Mapping):
        special.add("instructions")
    elif doc_loc is not None and doc_loc[0] == "top":
        special.add(doc_loc[1])

    for key, val in mapping.items():
        skey = str(key)
        if skey in special:
            continue
        if (
            (isinstance(val, (int, float, bool)) or val is None)
            or (isinstance(val, str) and encoded_response_size(val)[0] <= 256)
            or (isinstance(val, Mapping) and skey in {"summary", "cycle", "scope"} and encoded_response_size(val)[0] <= 1024)
        ):
            scalar_keys.append(skey)

    # 1. Summary / scalar metadata keys
    if scalar_keys:
        planned.extend(
            _plan_dict_fragments(
                "",
                mapping,
                page_frag_budget_bytes=page_frag_budget_bytes,
                page_frag_budget_chars=page_frag_budget_chars,
                non_subject_chunk_bytes=non_subject_chunk_bytes,
                non_subject_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
                subject_non_string_map=subject_non_string_map,
                key_order=scalar_keys,
            )
        )
        handled.update(scalar_keys)

    # 2 & 3 & 4. Primary instruction + Docs TOC + Doc bodies in dependency order
    if isinstance(mapping.get("instructions"), Mapping):
        instr = mapping["instructions"]
        docs_subkey = doc_loc[1] if (doc_loc and doc_loc[0] == "instructions") else None
        meta_keys = [
            str(k)
            for k in instr.keys()
            if str(k) not in {"content", docs_subkey}
        ]
        if meta_keys:
            planned.extend(
                _plan_dict_fragments(
                    "/instructions",
                    instr,
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                    key_order=meta_keys,
                )
            )
        if "content" in instr:
            planned.extend(
                _plan_value_fragments(
                    "/instructions/content",
                    instr["content"],
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                )
            )
        if docs_subkey is not None:
            planned.extend(
                _emit_docs_toc_and_bodies(
                    join_pointer("/instructions", docs_subkey),
                    list(instr[docs_subkey]),
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                )
            )
        handled.add("instructions")
    else:
        if "primary" in mapping:
            planned.extend(
                _plan_value_fragments(
                    "/primary",
                    mapping["primary"],
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                )
            )
            handled.add("primary")
        if doc_loc is not None and doc_loc[0] == "top":
            doc_key = doc_loc[1]
            planned.extend(
                _emit_docs_toc_and_bodies(
                    join_pointer("", doc_key),
                    list(mapping[doc_key]),
                    page_frag_budget_bytes=page_frag_budget_bytes,
                    page_frag_budget_chars=page_frag_budget_chars,
                    non_subject_chunk_bytes=non_subject_chunk_bytes,
                    non_subject_chunk_chars=non_subject_chunk_chars,
                    subject_pointer_map=subject_pointer_map,
                    subject_non_string_map=subject_non_string_map,
                )
            )
            handled.add(doc_key)

    # 5. Project context
    if "project" in mapping:
        planned.extend(
            _plan_value_fragments(
                "/project",
                mapping["project"],
                page_frag_budget_bytes=page_frag_budget_bytes,
                page_frag_budget_chars=page_frag_budget_chars,
                non_subject_chunk_bytes=non_subject_chunk_bytes,
                non_subject_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
                subject_non_string_map=subject_non_string_map,
            )
        )
        handled.add("project")

    # 6. Remaining fields
    remaining = [str(k) for k in mapping.keys() if str(k) not in handled]
    if remaining:
        planned.extend(
            _plan_dict_fragments(
                "",
                mapping,
                page_frag_budget_bytes=page_frag_budget_bytes,
                page_frag_budget_chars=page_frag_budget_chars,
                non_subject_chunk_bytes=non_subject_chunk_bytes,
                non_subject_chunk_chars=non_subject_chunk_chars,
                subject_pointer_map=subject_pointer_map,
                subject_non_string_map=subject_non_string_map,
                key_order=remaining,
            )
        )
    return planned


def _page_shell(
    *,
    response_id: str,
    origin: str,
    context: str | None = None,
    summary: dict[str, Any],
    result_revision: str,
    part: int,
    parts_total: int,
    fragments: list[dict[str, Any]],
    max_bytes: int,
    max_chars: int,
    transition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "paginated",
        "origin": origin,
        "summary": summary,
        "fragments": fragments,
        "page": {
            "format_version": PAGE_FORMAT_VERSION,
            "response_id": response_id,
            "part": part,
            "parts_total": parts_total,
            "complete": part == parts_total,
            "has_more": part < parts_total,
            "max_bytes": max_bytes,
            "max_chars": max_chars,
            "budget_counter": BUDGET_COUNTER,
            "result_revision": result_revision,
        },
    }
    if part < parts_total:
        next_call: dict[str, Any] = {
            "tool": "read",
            "operation": "core.page",
            "arguments": {"response_id": response_id, "part": part + 1},
        }
        if context is not None:
            next_call["context"] = context
        body["next_call"] = next_call
    return with_transition_first(body, transition)


def _available_fragment_budget(
    *,
    response_id: str,
    origin: str,
    context: str | None,
    summary: dict[str, Any],
    result_revision: str,
    max_bytes: int,
    max_chars: int,
    reserve_transition: bool,
) -> tuple[int, int]:
    empty_page = _page_shell(
        response_id=response_id,
        origin=origin,
        context=context,
        summary=summary,
        result_revision=result_revision,
        part=99998,
        parts_total=99999,
        fragments=[],
        max_bytes=max_bytes,
        max_chars=max_chars,
        transition=_transition_reservation_payload() if reserve_transition else None,
    )
    shell_bytes, shell_chars = encoded_response_size(empty_page)
    frag_bytes = max_bytes - shell_bytes
    frag_chars = max_chars - shell_chars
    if frag_bytes < 96 or frag_chars < 96:
        raise CoreStructuredError(
            "invalid_arguments",
            "Response page budget is too small to fit page metadata and fragments",
            operation="core.page",
            max_bytes=max_bytes,
            max_chars=max_chars,
        )
    return frag_bytes, frag_chars


def _pack_fragments_into_pages(
    planned: list[_PlannedFragment],
    *,
    response_id: str,
    origin: str,
    context: str | None,
    summary: dict[str, Any],
    result_revision: str,
    max_bytes: int,
    max_chars: int,
    reserve_transition: bool,
) -> list[list[_PlannedFragment]]:
    if not planned:
        return [[]]

    def fits_page_fragments(items: list[_PlannedFragment], page_num: int) -> bool:
        probe_part = max(99998, page_num)
        probe_total = max(99999, page_num + 1)
        probe = _page_shell(
            response_id=response_id,
            origin=origin,
            context=context,
            summary=summary,
            result_revision=result_revision,
            part=probe_part,
            parts_total=probe_total,
            fragments=[item.fragment for item in items],
            max_bytes=max_bytes,
            max_chars=max_chars,
            transition=_transition_reservation_payload() if reserve_transition else None,
        )
        return fits_response(probe, max_bytes=max_bytes, max_chars=max_chars)

    pages: list[list[_PlannedFragment]] = []
    current: list[_PlannedFragment] = []
    for entry in planned:
        candidate = [*current, entry]
        if fits_page_fragments(candidate, len(pages) + 1):
            current.append(entry)
            continue
        if current:
            pages.append(current)
            current = [entry]
            if not fits_page_fragments(current, len(pages) + 1):
                raise CoreStructuredError(
                    "invalid_arguments",
                    "Single fragment exceeds page response budget",
                    operation="core.page",
                    fragment_path=entry.fragment.get("path"),
                )
        else:
            raise CoreStructuredError(
                "invalid_arguments",
                "Single fragment exceeds page response budget",
                operation="core.page",
                fragment_path=entry.fragment.get("path"),
            )
    if current:
        pages.append(current)
    return pages


def build_snapshot(
    result: Any,
    *,
    origin: str,
    context: str | None = None,
    root_id: str = "",
    response_id: str | None = None,
    delivery_manifest: DeliveryManifest | None = None,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
    reserve_transition: bool | None = None,
) -> Snapshot:
    """Split an oversized result deterministically into a paginated Snapshot."""
    clean_result = {k: v for k, v in result.items() if k != "transition"} if isinstance(result, dict) else deepcopy(result)
    rid = response_id or secrets.token_hex(16)
    reserve = (delivery_manifest is not None) if reserve_transition is None else reserve_transition
    result_revision = canonical_result_revision(clean_result)
    summary = extract_summary(clean_result)

    frag_budget_bytes, frag_budget_chars = _available_fragment_budget(
        response_id=rid,
        origin=origin,
        context=context,
        summary=summary,
        result_revision=result_revision,
        max_bytes=max_bytes,
        max_chars=max_chars,
        reserve_transition=reserve,
    )

    internal_max_bytes, internal_max_chars = _compute_subject_internal_limits(
        clean_result,
        delivery_manifest,
        frag_budget_bytes=frag_budget_bytes,
        frag_budget_chars=frag_budget_chars,
    )
    subject_pointer_map: dict[str, tuple[int, list[tuple[int, str, dict[str, Any] | None]]]] = {}
    subject_non_string_map: dict[str, int] = {}
    subject_meta_layouts: list[tuple[DeliverySubjectManifest, str, int]] = []

    if delivery_manifest is not None:
        for subj_idx, subject in enumerate(delivery_manifest.subjects):
            layout_rev, parts_total, pointer_chunks = _compute_subject_internal_parts(
                clean_result,
                subject,
                internal_max_bytes=internal_max_bytes,
                internal_max_chars=internal_max_chars,
            )
            subject_meta_layouts.append((subject, layout_rev, parts_total))
            for ptr, chunks in pointer_chunks.items():
                subject_pointer_map[ptr] = (subj_idx, chunks)
            for ptr in subject.payload_paths:
                if ptr not in pointer_chunks and resolve_pointer(clean_result, ptr) is not _MISSING:
                    subject_non_string_map[ptr] = subj_idx

    non_subject_chunk_bytes = frag_budget_bytes
    non_subject_chunk_chars = frag_budget_chars

    if isinstance(clean_result, Mapping) and _is_startup_payload(origin, clean_result):
        planned = _plan_startup_fragments(
            clean_result,
            page_frag_budget_bytes=frag_budget_bytes,
            page_frag_budget_chars=frag_budget_chars,
            non_subject_chunk_bytes=non_subject_chunk_bytes,
            non_subject_chunk_chars=non_subject_chunk_chars,
            subject_pointer_map=subject_pointer_map,
            subject_non_string_map=subject_non_string_map,
        )
    else:
        planned = _plan_value_fragments(
            "",
            clean_result,
            page_frag_budget_bytes=frag_budget_bytes,
            page_frag_budget_chars=frag_budget_chars,
            non_subject_chunk_bytes=non_subject_chunk_bytes,
            non_subject_chunk_chars=non_subject_chunk_chars,
            subject_pointer_map=subject_pointer_map,
            subject_non_string_map=subject_non_string_map,
        )

    packed = _pack_fragments_into_pages(
        planned,
        response_id=rid,
        origin=origin,
        context=context,
        summary=summary,
        result_revision=result_revision,
        max_bytes=max_bytes,
        max_chars=max_chars,
        reserve_transition=reserve,
    )

    page_parts_by_subj: list[dict[int, set[int]]] = [{} for _ in subject_meta_layouts]
    final_pages: list[list[dict[str, Any]]] = []
    for page_num, page_entries in enumerate(packed, start=1):
        final_pages.append([entry.fragment for entry in page_entries])
        for entry in page_entries:
            if entry.subject_tag is None:
                continue
            s_idx, internal_part = entry.subject_tag
            subject = subject_meta_layouts[s_idx][0]
            if subject.eligible:
                page_parts_by_subj[s_idx].setdefault(page_num, set()).add(internal_part)

    subject_layouts = tuple(
        SubjectLayout(
            kind=subj.kind,
            id=subj.id,
            revision=subj.revision,
            layout_revision=layout_rev,
            parts_total=parts_total,
            page_parts={
                p: tuple(sorted(parts))
                for p, parts in page_parts_by_subj[idx].items()
            }
            if subj.eligible
            else {},
            context=subj.context,
            eligible=subj.eligible,
            metadata=subj.metadata,
        )
        for idx, (subj, layout_rev, parts_total) in enumerate(subject_meta_layouts)
    )

    return Snapshot(
        format_version=PAGE_FORMAT_VERSION,
        response_id=rid,
        origin=origin,
        context=context,
        root_id=root_id,
        result_revision=result_revision,
        max_bytes=max_bytes,
        max_chars=max_chars,
        reserve_transition=reserve,
        summary=summary,
        pages=tuple(final_pages),
        delivery_owner=delivery_manifest.owner if delivery_manifest else None,
        cycle_id=delivery_manifest.cycle_id if delivery_manifest else None,
        scope_id=delivery_manifest.scope_id if delivery_manifest else None,
        delivery_operation=(delivery_manifest.operation or origin) if delivery_manifest else None,
        delivery_metadata=deepcopy(delivery_manifest.metadata) if delivery_manifest else {},
        subjects=subject_layouts,
    )


def build_page(
    snapshot: Snapshot,
    part: int,
    *,
    transition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded page response dict for 1-based page index `part`."""
    if part < 1 or part > snapshot.parts_total:
        raise CoreStructuredError(
            "response_part_out_of_range",
            f"Requested page {part} is out of range (1..{snapshot.parts_total})",
            operation="core.page",
            response_id=snapshot.response_id,
            part=part,
            parts_total=snapshot.parts_total,
        )
    if not transition_fits_reservation(transition):
        raise CoreStructuredError(
            "delivery_unavailable",
            "Delivery transition exceeds 1024-byte JIT reservation",
            operation="core.page",
            response_id=snapshot.response_id,
            part=part,
        )
    page = _page_shell(
        response_id=snapshot.response_id,
        origin=snapshot.origin,
        context=snapshot.context,
        summary=deepcopy(snapshot.summary),
        result_revision=snapshot.result_revision,
        part=part,
        parts_total=snapshot.parts_total,
        fragments=deepcopy(snapshot.pages[part - 1]),
        max_bytes=snapshot.max_bytes,
        max_chars=snapshot.max_chars,
        transition=transition,
    )
    if not fits_response(page, max_bytes=snapshot.max_bytes, max_chars=snapshot.max_chars):
        raise CoreStructuredError(
            "response_corrupt",
            "Constructed response page exceeds configured size budget",
            operation="core.page",
            response_id=snapshot.response_id,
            part=part,
        )
    return page


def build_receipt(snapshot: Snapshot, part: int) -> DeliveryReceipt:
    """Build the DeliveryReceipt for one issued page of a paginated snapshot."""
    if not snapshot.delivery_owner:
        raise ValueError("Snapshot has no delivery_owner")
    subjects = tuple(
        DeliverySubjectReceipt(
            kind=subj.kind,
            id=subj.id,
            revision=subj.revision,
            layout_revision=subj.layout_revision,
            parts_total=subj.parts_total,
            issued_parts=subj.page_parts.get(part, ()) if subj.eligible else (),
            context=subj.context,
            eligible=subj.eligible,
            metadata=subj.metadata,
        )
        for subj in snapshot.subjects
    )
    return DeliveryReceipt(
        response_id=snapshot.response_id,
        part=part,
        parts_total=snapshot.parts_total,
        complete=(part == snapshot.parts_total),
        owner=snapshot.delivery_owner,
        context=snapshot.context,
        cycle_id=snapshot.cycle_id,
        scope_id=snapshot.scope_id,
        operation=snapshot.delivery_operation or snapshot.origin,
        subjects=subjects,
        metadata=snapshot.delivery_metadata,
    )


def build_single_delivery_receipt(
    result: Any,
    delivery_manifest: DeliveryManifest,
    *,
    origin: str | None = None,
    response_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
) -> DeliveryReceipt:
    """Build a complete single-page DeliveryReceipt for an unpaginated operation."""
    clean_result = {k: v for k, v in result.items() if k != "transition"} if isinstance(result, dict) else result
    rid = response_id or secrets.token_hex(16)
    frag_budget_bytes, frag_budget_chars = _available_fragment_budget(
        response_id=rid,
        origin=origin or delivery_manifest.operation or "op",
        context=delivery_manifest.context,
        summary=extract_summary(clean_result),
        result_revision=canonical_result_revision(clean_result),
        max_bytes=max_bytes,
        max_chars=max_chars,
        reserve_transition=True,
    )
    internal_max_bytes, internal_max_chars = _compute_subject_internal_limits(
        clean_result,
        delivery_manifest,
        frag_budget_bytes=frag_budget_bytes,
        frag_budget_chars=frag_budget_chars,
    )
    subject_receipts: list[DeliverySubjectReceipt] = []
    for subj in delivery_manifest.subjects:
        layout_rev, parts_total, pointer_chunks = _compute_subject_internal_parts(
            clean_result,
            subj,
            internal_max_bytes=internal_max_bytes,
            internal_max_chars=internal_max_chars,
        )
        has_payload = bool(pointer_chunks) or any(
            resolve_pointer(clean_result, p) is not _MISSING for p in subj.payload_paths
        )
        issued = tuple(range(1, parts_total + 1)) if (subj.eligible and has_payload) else ()
        subject_receipts.append(
            DeliverySubjectReceipt(
                kind=subj.kind,
                id=subj.id,
                revision=subj.revision,
                layout_revision=layout_rev,
                parts_total=parts_total,
                issued_parts=issued,
                context=subj.context,
                eligible=subj.eligible,
                metadata=subj.metadata,
            )
        )
    return DeliveryReceipt(
        response_id=rid,
        part=1,
        parts_total=1,
        complete=True,
        owner=delivery_manifest.owner,
        context=delivery_manifest.context,
        cycle_id=delivery_manifest.cycle_id,
        scope_id=delivery_manifest.scope_id,
        operation=delivery_manifest.operation or origin,
        subjects=tuple(subject_receipts),
        metadata=delivery_manifest.metadata,
    )


def _set_at_pointer(
    root: Any,
    pointer: str,
    val: Any,
    array_lengths: dict[str, int],
) -> Any:
    segments = split_pointer(pointer)
    if not segments:
        return val
    if root is _MISSING:
        root = [_MISSING] * array_lengths[""] if "" in array_lengths else {}

    cur = root
    prefix = ""
    for depth, seg in enumerate(segments[:-1]):
        next_prefix = join_pointer(prefix, seg)
        default_child: Any = (
            [_MISSING] * array_lengths[next_prefix]
            if next_prefix in array_lengths
            else {}
        )
        if isinstance(cur, list):
            idx = int(seg)
            while len(cur) <= idx:
                cur.append(_MISSING)
            if cur[idx] is _MISSING:
                cur[idx] = default_child
            cur = cur[idx]
        else:
            if seg not in cur or cur[seg] is _MISSING:
                cur[seg] = default_child
            cur = cur[seg]
        prefix = next_prefix

    last = segments[-1]
    if isinstance(cur, list):
        idx = int(last)
        while len(cur) <= idx:
            cur.append(_MISSING)
        cur[idx] = val
    else:
        cur[last] = val
    return root


def _ensure_container(
    root: Any,
    pointer: str,
    is_array: bool,
    length: int | None,
    array_lengths: dict[str, int],
) -> tuple[Any, Any]:
    segments = split_pointer(pointer)
    if not segments:
        if root is _MISSING:
            root = [_MISSING] * (length or 0) if is_array else {}
        return root, root
    if root is _MISSING:
        root = [_MISSING] * array_lengths[""] if "" in array_lengths else {}
    cur = root
    prefix = ""
    for idx_seg, seg in enumerate(segments):
        next_prefix = join_pointer(prefix, seg)
        is_last = idx_seg == len(segments) - 1
        if is_last:
            child_default: Any = [_MISSING] * (length or 0) if is_array else {}
        else:
            child_default = [_MISSING] * array_lengths[next_prefix] if next_prefix in array_lengths else {}
        if isinstance(cur, list):
            pos = int(seg)
            while len(cur) <= pos:
                cur.append(_MISSING)
            if cur[pos] is _MISSING:
                cur[pos] = child_default
            cur = cur[pos]
        else:
            if seg not in cur or cur[seg] is _MISSING:
                cur[seg] = child_default
            cur = cur[seg]
        prefix = next_prefix
    return root, cur


def _strip_missing_sentinels(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_missing_sentinels(v) for k, v in value.items() if v is not _MISSING}
    if isinstance(value, list):
        return [_strip_missing_sentinels(v) for v in value]
    return value


def reconstruct_from_pages(pages: Sequence[Mapping[str, Any]]) -> Any:
    """Reconstruct the full operation result from collected paginated pages 1..N."""
    if not pages:
        raise ValueError("Cannot reconstruct result from empty page sequence")

    ordered = sorted(pages, key=lambda p: int(p["page"]["part"]))
    first_meta = ordered[0]["page"]
    expected_id = first_meta["response_id"]
    expected_total = int(first_meta["parts_total"])
    expected_rev = first_meta.get("result_revision")

    if len(ordered) != expected_total:
        raise ValueError(f"Incomplete pages: got {len(ordered)} of {expected_total}")
    for expected_part, page in enumerate(ordered, start=1):
        meta = page["page"]
        if meta["response_id"] != expected_id:
            raise ValueError("Pages belong to different response_id snapshots")
        if int(meta["part"]) != expected_part or int(meta["parts_total"]) != expected_total:
            raise ValueError(f"Expected page {expected_part}/{expected_total}, got {meta['part']}/{meta['parts_total']}")
        if expected_rev is not None and meta.get("result_revision") != expected_rev:
            raise ValueError("Page result_revision mismatch during reconstruction")

    array_lengths: dict[str, int] = {}
    all_fragments: list[dict[str, Any]] = []
    for page in ordered:
        for frag in page.get("fragments", ()):
            all_fragments.append(frag)
            if frag.get("kind") == "array":
                array_lengths[frag["path"]] = max(
                    array_lengths.get(frag["path"], 0),
                    int(frag["length"]),
                )

    root: Any = _MISSING
    text_parts: dict[str, dict[int, tuple[int, str]]] = {}

    for frag in all_fragments:
        kind = frag["kind"]
        path = frag["path"]
        if kind == "value":
            root = _set_at_pointer(root, path, deepcopy(frag["value"]), array_lengths)
        elif kind == "object":
            root, container = _ensure_container(root, path, False, None, array_lengths)
            values = frag["value"]
            for key in frag.get("keys", values.keys()):
                container[key] = deepcopy(values[key])
        elif kind == "array":
            length = int(frag["length"])
            start = int(frag["start"])
            root, container = _ensure_container(root, path, True, length, array_lengths)
            while len(container) < length:
                container.append(_MISSING)
            for offset, item in enumerate(frag["items"]):
                container[start + offset] = deepcopy(item)
        elif kind == "text":
            bucket = text_parts.setdefault(path, {})
            part_no = int(frag["part"])
            total_parts = int(frag["parts"])
            bucket[part_no] = (total_parts, frag["text"])
        else:
            raise ValueError(f"Unsupported fragment kind: {kind!r}")

    for path, parts_map in text_parts.items():
        if not parts_map:
            continue
        expected_parts = next(iter(parts_map.values()))[0]
        if len(parts_map) != expected_parts:
            raise ValueError(f"Incomplete text fragments at {path}: got {len(parts_map)}/{expected_parts}")
        assembled = "".join(parts_map[i][1] for i in range(1, expected_parts + 1))
        root = _set_at_pointer(root, path, assembled, array_lengths)

    if root is _MISSING:
        return {}
    return _strip_missing_sentinels(root)
