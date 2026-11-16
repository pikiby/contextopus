"""Read-only library contribution for Core-owned markdown collections."""

from .link_codec import strip_wikilink_wrappers
from .links import collect_relations
from ..rag import DocumentKey


def _summary(record):
    collection = record["collection"]
    rel = record.get("path", "")
    doc_type = record.get("metadata", {}).get("type") or (
        "instruction" if collection == "instructions" else collection
    )
    full_path = (
        f"core/instructions/{rel}"
        if collection == "instructions"
        else f"core/documents/{collection}/{rel}"
    )
    return {
        "key": ["core", collection, record["id"]],
        "collection": collection,
        "title": record.get("title", record["id"]),
        "description": record.get("description", ""),
        "type": doc_type,
        "path": full_path,
        "revision": record.get("revision"),
    }


def _collection_ids(collections):
    return [c["id"] if isinstance(c, dict) else c for c in collections.collections()]


def _resolve_link_target(collections, source_collection, ref):
    raw = LinkCodec.strip_wrapper(ref)
    if not raw:
        return None
    col_ids = _collection_ids(collections)
    if raw.startswith("core/"):
        rest = raw.removeprefix("core/")
        if "/" in rest:
            target_col, target_ref = rest.split("/", 1)
            if target_col in col_ids:
                return target_col, target_ref
    for col in [source_collection, *col_ids]:
        if raw.startswith(col + "/"):
            return col, raw.removeprefix(col + "/")
    return source_collection, raw


def browse_documents(
    collections,
    action,
    collection="",
    query="",
    mode="text",
    type="",
    offset=0,
    limit=40,
    id="",
):
    """List, search or read Core document collections without modifying their sources."""
    if action not in {"collections", "records", "record"}:
        raise ValueError("Unknown library action")
    if mode not in {"text", "semantic"}:
        raise ValueError("Unknown search mode")
    if not 1 <= limit <= 100 or offset < 0 or offset > 100000:
        raise ValueError("Invalid pagination")

    available = _collection_ids(collections)
    if collection and collection not in available:
        raise ValueError("Unknown Core collection: " + collection)

    if action == "collections":
        return {
            "collections": [
                {"id": name, "title": name.replace("_", " ").replace("-", " ").title()}
                for name in available
            ]
        }

    if action == "record":
        if not id:
            raise ValueError("Record id is required")
        target_col = collection
        target_ref = id
        if not target_col:
            if target_ref.startswith("core/"):
                rest = target_ref.removeprefix("core/")
                if "/" in rest:
                    target_col, target_ref = rest.split("/", 1)
            elif "/" in target_ref and target_ref.split("/", 1)[0] in available:
                target_col, target_ref = target_ref.split("/", 1)
            elif "instructions" in available:
                target_col = "instructions"
            else:
                raise ValueError("Collection is required")
        record = collections.read(target_col, target_ref)
        links = []
        relations = collect_relations(collections, DocumentKey("core", target_col, record["id"]))
        for item in relations.get("outgoing", []):
            k = item["key"]
            target = collections.read(k["collection"], k["id"])
            links.append(
                {
                    "key": [k["module"], k["collection"], k["id"]],
                    "title": target["title"],
                    "description": target["description"],
                }
            )
        for item in relations.get("unresolved", []):
            links.append(
                {
                    "title": item["reference"],
                    "reference": item["reference"],
                    "unavailable": True,
                }
            )
        return {
            **_summary(record),
            "content": record["content"],
            "metadata": record.get("metadata", {}),
            "links": links,
        }

    diagnostics = None
    target_collections = [collection] if collection else available
    if mode == "semantic" and query.strip() and collections.index is not None:
        response = collections.index.search_detailed(
            query,
            module="core",
            collection=collection or None,
        )
        records = []
        for hit in response.get("results", []):
            key = hit.get("key", [])
            if len(key) < 3 or key[0] != "core":
                continue
            hit_col = key[1]
            hit_id = key[2]
            metadata = hit.get("metadata", {})
            record = {
                "collection": hit_col,
                "id": hit_id,
                "title": metadata.get("title", hit_id),
                "description": hit.get("description", metadata.get("description", "")),
                "path": hit.get("path", ""),
                "revision": hit.get("revision"),
                "metadata": metadata,
            }
            records.append({**_summary(record), "distance": hit.get("distance")})
        diagnostics = response.get("diagnostics")
    else:
        records = []
        for col in target_collections:
            for item in collections.list(col):
                records.append(_summary(item))
        needle = query.strip().casefold()
        if needle:
            records = [
                r
                for r in records
                if needle in (r["title"] + " " + r["description"]).casefold()
            ]
        records.sort(key=lambda r: (r["title"].casefold(), r["key"]))

    types = sorted({r["type"] for r in records}) if records else (
        ["instruction"] if collection == "instructions" else []
    )
    if type:
        records = [r for r in records if r["type"] == type]

    result = {
        "records": records[offset : offset + limit],
        "total": len(records),
        "has_more": offset + limit < len(records),
        "types": types,
    }
    if diagnostics is not None:
        result["diagnostics"] = diagnostics
    return result


def browse_instructions(library, action, collection="", query="", mode="text",
                        type="", offset=0, limit=40, id=""):
    """Compatibility wrapper forwarding to browse_documents via library.collections."""
    collections = getattr(library, "collections", library)
    return browse_documents(
        collections,
        action,
        collection=collection,
        query=query,
        mode=mode,
        type=type,
        offset=offset,
        limit=limit,
        id=id,
    )
