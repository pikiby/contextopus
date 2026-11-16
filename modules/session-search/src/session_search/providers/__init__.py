"""Isolated source adapters; providers never write or index client files."""
from .codex import CodexProvider
from .claude_code import ClaudeProvider
from .antigravity import AntigravityProvider
from .opencode import OpenCodeProvider
from .crush import CrushProvider

PROVIDERS = {
    p.name: p
    for p in (CodexProvider(), ClaudeProvider(), AntigravityProvider(), OpenCodeProvider(), CrushProvider())
}
