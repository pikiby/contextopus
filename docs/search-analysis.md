# Search evidence and analysis

Core records structured operation and search telemetry in `<root>/core/analytics/operations.sqlite3` so you can audit why a past search kept or cut specific records and replay candidate pools counterfactually without re-embedding current files.

## What Core retains

Core's `search_evidence` table links each search event ID in the operation journal with:

- Algorithm identifier/version, effective parameters and parameter origins
- Eligibility scope, candidate/fragment/material counts, cut reasons and boundary distances
- Up to **1,000 candidate metadata rows** (`[module, collection, object_id]`, revisions, part numbers, distances and selected flags)
- Query hash, caller-supplied `task_id` / `purpose` and resolved instruction revision

**Privacy & storage boundaries:** Raw search text, vector embeddings and Markdown bodies are **not** copied into `search_evidence`. Git history under `<root>/core/.git/` retains committed Markdown and settings revisions separately.

Compact search responses omit candidate arrays; use `core.search_event` to inspect retained metadata for a specific event. Also compare `candidate_snapshot_truncated` against `retrieval_truncated`: a search might retain 100% of a candidate pool truncated by `candidate_limit`, or retain 1,000 rows of a larger pool — neither guarantees coverage of all unindexed files.

---

## Query bounded episodes (`read`)

1. List recent search events (defaulting to a frozen 7-day window if `since`/`until` are omitted):

```json
{"operation": "core.search_events", "arguments": {"context": "my-app", "task_id": "task-42", "limit": 20}}
```

Reuse the returned `since`, `until` and `next_offset` values for consistent paging, or filter by `instructions_revision` to isolate runs that happened under one rule set.

2. Inspect candidate rows and selection flags for one event:

```json
{"operation": "core.search_event", "arguments": {"event_id": "<returned-event-id>"}}
```

3. Check stored vector counts without loading the embedding model:

```json
{"operation": "core.index_stats", "arguments": {"module": "core", "collection": "notes"}}
```

---

## Counterfactual replay (`core.search_replay`)

Replay re-runs the **current implementation** of any installed selection algorithm against a past event's stored candidate snapshot without generating embeddings or querying today's corpus:

```json
{
  "operation": "core.search_replay",
  "arguments": {
    "event_id": "<returned-event-id>",
    "selection": {"algorithm": "nearest_n", "parameters": {"N": 8}},
    "relevant_keys": [["core", "notes", "retry-policy"]]
  }
}
```

Key replay fields:
- `replay_kind: "current_counterfactual"` and `original_execution_reproduced: false`: makes explicit that replay compares current code against the retained candidate distances rather than reconstructing legacy binary behavior.
- `added` / `removed`: materials or fragments gained or dropped vs the originally recorded selections.
- `candidate_snapshot_truncated`, `retrieval_truncated`, `original_selection_complete`: clarify whether any selected/rejected rows exceeded the stored 1,000-row cap.
- `relevant_keys` (material mode only): when supplied, computes precision/recall metrics strictly against those caller-labelled relevant keys (fragment-mode replay reports diffs without label scoring).

---

## Interpreting results accurately

| Question | Required evidence |
|---|---|
| Was selection too narrow? | Relevant candidates present in `considered` but cut by `distance_jump`/`d_max`, or broader retrieval needed if `candidate_limit` truncated the pool |
| Was selection too broad? | Irrelevant kept candidates measured against the real task question |
| Did parameter tuning help? | Same-pool replay precision/recall against `relevant_keys`, separate from corpus growth comparisons |
| Were rules followed? | Exact instruction revision active at run time + observed MCP reads/writes |
| Were records indexed at the time? | Compare `eligible_documents` / `core.index_stats` against current disk files (`core.documents_reconcile`) |

See [Search selection algorithms](search-selection.md).
