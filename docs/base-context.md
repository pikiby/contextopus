# Contexts, instructions and route maps

In Contextopus, a **Context** is a regular Markdown + YAML document stored under `<root>/core/documents/contexts/`. Contexts organize project knowledge, bind mandatory rules, associate working directories (`workdirs`) and maintain isolated search and safety settings — all sharing one Core index and runtime.

## Context metadata fields

Example context header (`<root>/core/documents/contexts/my-app/Context.md`):

```markdown
---
id: my-app
slug: my-app
type: context
title: My App
description: Backend API service and deployment rules
workdirs:
  - /home/user/projects/my-app
  - /home/user/projects/my-app-docs
requires_rule:
  - "[[common-safety]]"
instructions:
  project: "[[my-app-rules]]"
rule_routes:
  - when: Working on database schema changes or migrations
    target: "[[db-migration-policy]]"
requires_read:
  - "[[architecture-overview]]"
references:
  - "[[deployment-checklist]]"
---
High-level project overview and scope boundaries...
```

## How `core.context_read` resolves a request

1. **Identity or working directory resolution (`id` / `cwd`):**
   - Passing `id` (slug or stable ID) reads that context directly from `core/documents/contexts/`.
   - When resolving by `cwd` (or an absolute path in `id`), `ContextReader` inspects `workdirs` and legacy `workdir` across all context documents using `PurePosixPath.is_relative_to(...)` and selects the **deepest matching directory prefix** (`max` path segment count), returning `resolved_workdir` alongside the context.
   - Traversal (`..`) inside `workdirs` is ignored safely.
2. **Resolved rules (`rules` & `rules_revision`):**
   - Collects entries from `requires_rule` and `instructions` role mappings, resolves their transitive instruction dependency graph in deterministic order via `core.instructions_resolve`, and returns all rule bodies plus an aggregate `rules_revision`.
3. **Topic route map (`route_map`):**
   - Combines explicit `rule_routes` entries (`when` condition + `target` link) and any declared `references`/`links`.
   - `routing_directive` instructs agents to open every `route_map` entry whose `when` or `title` matches the current sub-task via `core.library(action="record", ...)` and treat it as a mandatory rule for that task.
4. **Reference preloads (`required_reads`):**
   - Resolves `requires_read` documents cross-collection and includes their full content marked `"activation": "data_only"`, keeping factual reference data clearly separated from active rules.
5. **Context settings (`settings`):**
   - Returns effective context configuration merged from root and `core/configuration/contexts/core/<context-id>.json` (`documents_approval_required`, `instructions_approval_required`, `git_history_enabled`, `selection`, etc.).

## Isolation between contexts

- Reading a record from another context as evidence via `core.search` or `core.library` **never activates that other context's instructions**.
- Search settings default to the active context's configuration (`core.settings_preview` / `core.settings_apply` with `context="<id>"`).
- Per-context safety toggles (`documents_approval_required`, `instructions_approval_required`, `git_history_enabled`) can be adjusted either via Core settings operations or directly in the Web UI under **Contexts and rules**.

See [Instruction library](instruction-library.md) and [Using Contextopus](usage.md).
