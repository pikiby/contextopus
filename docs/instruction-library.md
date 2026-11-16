# Shared instruction documents

Core owns canonical instruction files under `<root>/core/instructions/`. Each UTF-8 Markdown file declares YAML front matter followed by its complete instruction text:

```markdown
---
id: coding.project-update
type: instruction
title: Project update rules
description: Rules for updating project records safely
config:
  approval: explicit
instructions:
  common: "[[common]]"
requires_rule:
  - "[[verification-standards]]"
---
Read the current project record before updating it.
```

## Schema rules and validation

- `type` must be `"instruction"`.
- `id` provides a stable identity across file moves/renames; `title` and `description` must be strings when present.
- `config` must be a JSON-serializable mapping when present.
- `requires_rule`, `requires_read` and `references` must be lists of non-empty reference strings when present.
- `instructions` must map string role names to Obsidian-style wikilinks (`[[target]]` or `[[target|label]]`). Only YAML dependency fields (`instructions` / `requires_rule`) create automatic dependency edges; inline Markdown links inside the body remain navigational only.
- The instruction body is returned verbatim without rewriting and may itself contain Markdown headings or front matter.

Root context documents select instruction files using `requires_rule` or `instructions` mappings (for example `instructions: {work: "[[coding.project-update]]"}`), resolved whenever `core.context_read` loads that context.

## Operations

All interfaces use Core's unified registry (`read` for queries, `write` for mutations):

| Operation | Kind | Arguments | Result |
|---|---|---|---|
| `core.instructions_list` | `query` | none | Compact `instructions` records (`id`, `type`, `title`, `description`, `path`, `revision`) |
| `core.instructions_read` | `query` | `reference` | Full instruction record matching ID, relative path, filename stem or wikilink |
| `core.instructions_resolve` | `query` | `references: string[]` | Ordered full records with dependencies loaded once before dependants + aggregate `revision` |
| `core.documents_preview` | `query` | `collection="instructions"`, `path`, `metadata`, `content`, `expected_revision`, `action` | Validation result, unified diff and `action_contract` (`change_revision`, `approval_required`) |
| `core.documents_save` | `execute` | `collection="instructions"`, `path`, `metadata`, `content`, `expected_revision`, `approval?` | Atomic save, optional Git commit and `index` status |
| `core.documents_delete` | `execute` | `collection="instructions"`, `path`, `expected_revision`, `approval?` | Revision-checked deletion under approval policy |
| `core.documents_reconcile` | `execute` | `collection="instructions"` *(or null for all)* | Reconcile external disk edits/deletions with the shared RAG index |

## Revisions, approval policy and indexing

- `revision` is computed over the complete file bytes (including YAML front matter). Passing `expected_revision: null` creates a new file only; updating or deleting an existing instruction requires its exact current `revision`.
- Writes use flushed temporary files and atomic renames under the root advisory lock, rejecting path traversal, symlinks, hidden paths and duplicate IDs.
- By default, **`instructions_approval_required` is `true`**, meaning `core.documents_save` / `core.documents_delete` on instructions requires presenting the preview diff to the user and passing `approval={"change_revision": ..., "confirmation": ...}` from `core.documents_preview`.
- Saving Markdown never requires an embedding model to be active or loaded: if no embedding profile is configured (or indexing fails), the file still saves safely and returns pending index status (`core.documents_reconcile` retries indexing later once a model profile is configured).
- Direct Python callers can access `contextopus.core.documents.instructions.InstructionLibrary` (or `DocumentCollections.store("instructions")`), while MCP/Web/CLI clients share the exact same operations through Core.

See [Contexts and route maps](base-context.md) and [Using Contextopus](usage.md).
