---
operations: ["core.model_hardware", "core.model_plan", "core.model_apply", "core.settings", "core.settings_preview", "core.settings_apply"]
---

# Embedding configuration

Read active provider, profile, and chunking from `core.settings` (`active_embedding_config`, `active_chunking`). Preview changes with `core.model_plan` and apply the reviewed plan with `core.model_apply`. Embedding profile and chunking belong to the common root: switching rebuilds every enabled RAG source into an inactive table and swaps atomically on success (preserving old index on failure).

## Supported Providers & Defaults
- **Local default:** `sentence-transformers` on CPU, `Qwen/Qwen3-Embedding-0.6B` (rev `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1024 dims, `float32`, `l2`, `qwen3` format, max 32768 tokens), chunking `chunk_size_tokens: 512`, `overlap_tokens: 64`, `tokenizer: {"type": "provider"}`. Inspect cache/download scope per `guide(procedure="setup")` before uncached downloads.
- **Remote providers:** `ollama` (`/api/embed`, `truncate: false`) and `openai-compatible` (`/v1/embeddings`). Configure matching tokenizer (`huggingface` or `tiktoken`) and store only the `api_key_env` variable name, never literal API keys.
- Search thresholds (`selection`, `distance`, `searchN`) are changed via `core.settings_preview` / `core.settings_apply`. Report actual provider/dependency errors; never substitute vectors or another provider silently.
