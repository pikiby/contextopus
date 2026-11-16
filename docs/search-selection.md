# Search scope and result selection

Core applies structural eligibility filters before semantic nearest-neighbor retrieval. The active selection algorithm then cuts or pages the eligible candidate pool.

- `output: "materials"` (default) groups fragments by document key (`[module, collection, object_id]`) before selection, using the nearest fragment to represent each document for ranking while omitting full body text from search hits.
- `output: "fragments"` selects individual chunks directly, including their fragment text.

Cross-context search inherits the active context's search settings (`diagnostics.scope.settings_context`) unless overridden, while `scope.contexts` lists the collections/contexts actually scanned. Searching another context as evidence never activates that context's rules.

---

## Installed selection algorithms

Inspect `core.algorithms` for live descriptions and JSON parameter schemas. Distances are ranking measurements, not calibrated probability or confidence scores.

| Algorithm | Parameters and defaults | Selection order |
|---|---|---|
| `distance_jump` | `N: 20`, `d_max: 0.8`, `min_jump: 0.02`, `multiplier: 3.0` | Requested units -> distance cap -> first large gap over all remaining units -> N result cap |
| `distance_threshold` | `N: 20`, `d_max: 0.8` | Requested units -> distance cap -> N result cap |
| `nearest_n` | `N: 20` | Nearest N requested units without a relevance cut |
| `none` | `offset: 0`, `page_size: 20` | Distance-ordered page of requested units without a relevance cut |
| `archivist` | `d_max: null`, `top_k: 20` | Requested units -> optional distance cap -> upper-median first jump -> weak-plateau cut -> `top_k` result cap |

Built-in algorithms implement version `2.0.0` under selection contract `2`.

### Algorithm behavior notes

- **Material grouping first:** In material mode, every algorithm receives one nearest representative per document key before evaluating thresholds, gaps or result limits.
- **`distance_jump`:** Requires at least three units after `d_max`. Cuts before the first positive gap >= `max(min_jump, arithmetic_median(positive_gaps) * multiplier)`, then applies `N`. Diagnostics distinguish `insufficient_candidates`, `no_positive_gaps`, `no_jump`, `distance_jump` and `result_cap`.
- **`archivist`:** First-jump pass uses `max(0.02, upper_median(positive_gaps) * 3)`. If more than three units remain, a weak-plateau pass computes `head = median(first_three_distances)` and `delta = max(0.02, arithmetic_median(positive_gaps) * 2)`, cutting before the first item from index 2 onward whose distance exceeds `head + delta` while its preceding local gap is under `0.01`, followed by `top_k`.
- **`none`:** `offset` and `page_size` count distinct documents in material mode and individual chunks in fragment mode, preventing a single multi-chunk document from repeating within a page.

---

## Configuration layers and preview/apply

Common configuration shape:

```json
{
  "selection": {
    "algorithm": "distance_jump",
    "parameters": {
      "N": 20,
      "d_max": 0.8,
      "min_jump": 0.02,
      "multiplier": 3.0
    }
  }
}
```

Effective settings merge in this precedence order:

1. Built-in algorithm defaults (plus active index defaults if present).
2. `<root>/core/configuration/settings.json`
3. `<root>/core/configuration/modules/<module>.json`
4. `<root>/core/configuration/contexts/<module>/<context>.json`
5. Per-request `selection` overrides passed to `core.search`.

Partial parameter overrides apply only while keeping the same `algorithm`; changing `algorithm` resets unspecified parameters to the new algorithm's defaults. Invalid types, unknown algorithms and nonfinite values fail validation closed.

`core.settings` reports effective `values`, per-field `sources` (`algorithm.default`, `root.configuration`, `module.configuration`, `context.configuration`, `request`) and `revision`. Persistent updates go through `core.settings_preview` -> `core.settings_apply` with the matching `preview_revision` (and confirmation where required).

---

## Diagnostics and three independent limits

Search responses report scope, algorithm version, unit counts and completeness reasons:

- `eligible_documents` / `eligible_fragments` — indexed items permitted by structural filters before vector search.
- `considered_fragments` / `considered_documents` — finite-distance retrieved candidates and distinct keys.
- `selection_units`, `selected_fragments`, `selected_materials` — units fed to selection and kept counts.
- `invalid_distance_fragments` — rows skipped for missing or nonfinite distances.

Three independent bounds are separated explicitly:

1. **Candidate retrieval (`candidate_limit`):** Optional cap (1–100000) before grouping (`candidate_pool_truncated` / `completeness.reasons: ["candidate_limit"]`). Raising `N` cannot recover items excluded at retrieval.
2. **Relevance and response cap:** `boundary.relevance` records relevance cuts independently of `boundary.result_cap` (`limits.result`). `nearest_n` and `none` state explicitly that relevance was not evaluated.
3. **Evidence snapshot (`limits.candidate_snapshot`):** At most 1,000 candidate metadata rows are persisted per search event in Core analytics (`completeness.candidate_snapshot_complete`).

Note that `completeness.retrieval_complete` applies strictly to the **structurally eligible indexed corpus** — pending/unindexed files or source errors must be inspected via `core.documents_reconcile` or module status. Distance alone never validates freshness.

---

## Algorithm catalog and custom providers

- `core.algorithms(query=..., semantic=true|false)` lists installed descriptions or searches derivative cards indexed under module `core`, collection `search_algorithm` (internally using fixed `nearest_n`). Missing or stale cards return `catalog_state: "missing"` or `"stale"` with `next_action: "core.algorithms_refresh"`.
- Custom module providers declare `filter_algorithms` in `module.json`, supplying pairs of `description.json` + `algorithm.py` exporting `select(rows, parameters)` -> `(kept_rows, boundary_metadata)`. Code runs only when the module is enabled and its prepared fingerprint matches.
- **Repairing an unavailable algorithm:** If disabling or updating a module removes a saved custom algorithm, `core.settings` preserves the setting, flags `selection_status.valid: false` (`code: "algorithm_unavailable"`), and fails searches using it closed rather than silently substituting another algorithm. Re-enable the module or preview/apply a replacement selection via `core.settings_preview` / `core.settings_apply`.

---

## MCP examples

Inspect an algorithm schema without loading embeddings (`read`):

```json
{"operation": "core.algorithms", "arguments": {"query": "archivist"}}
```

Search across specific collections with an inline request override (`read`):

```json
{"operation": "core.search", "arguments": {"query": "release conditions", "collection": "notes", "selection": {"algorithm": "nearest_n", "parameters": {"N": 5}}}}
```

Inspect a paginated slice of raw fragments across scopes (`read`):

```json
{"operation": "core.search", "arguments": {"query": "release conditions", "output": "fragments", "selection": {"algorithm": "none", "parameters": {"offset": 20, "page_size": 20}}}}
```

Preview and save persistent selection settings for a context (`read` then `write`):

```json
{"operation": "core.settings_preview", "arguments": {"module": "core", "context": "project-a", "changes": {"selection": {"algorithm": "distance_threshold", "parameters": {"N": 40, "d_max": 0.6}}}}}
```

```json
{"operation": "core.settings_apply", "arguments": {"preview_revision": "<returned-preview-revision>"}}
```

Refresh the semantic algorithm catalog (`write`) and query it (`read`):

```json
{"operation": "core.algorithms_refresh", "arguments": {}}
```

```json
{"operation": "core.algorithms", "arguments": {"query": "retain a close cluster and cut a weak tail", "semantic": true, "limit": 3}}
```

See [Search evidence and analysis](search-analysis.md) and [Session Search](../modules/session-search/README.md) (which uses SQLite FTS5 lexical search instead of vector selection).
