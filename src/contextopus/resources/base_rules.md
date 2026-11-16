# Contextopus base rules

- `guide()` routes tasks and returns these rules; `guide(procedure=...)` loads a procedure once per task; `guide(operation=...)` returns a missing schema only.
- `read` queries; `write` mutates. Pass operation parameters inside `arguments` and the selected context name in `context` (never pass filesystem paths). Never bypass Contextopus MCP with shell/local storage reads; report connection failures.
- Records are Markdown + YAML guarded by `revision`. Ordinary links are navigation; routed context standards (`route_map` / `requires_rule`) are mandatory rules when their condition matches the task.
- Distinguish verified facts, hypotheses, user decisions, and proposals. Never save unverified trial parameters, temporary heuristics, or workarounds as permanent standards.
- Reading, searching, or general "continue/finish" remarks never authorize writes; follow active approval policies strictly.
