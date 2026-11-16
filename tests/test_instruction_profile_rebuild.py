"""Core-only profile rebuilding preserves its authoritative instruction sources."""

import copy
import pytest
from contextopus.startup import Application
from contextopus.core.resources import ProcessResources
from contextopus.core.embeddings import default_config
from contextopus.core.chunking import default_chunking
from fake_embeddings import Factory


def test_core_profile_switch_preserves_instructions(tmp_path):
    factory = Factory()
    with ProcessResources(factory) as resources:
        with Application(tmp_path, resources=resources, discover=False) as app:
            config = default_config()
            config["profile"]["dimensions"] = 2
            app.index.embeddings = config
            app.index._generic_table(create=True)
            saved = app.collections.save("instructions", "rules.md", {"id": "rules", "type": "instruction", "description": "Project rules"}, "Preserve user boundaries.")
            assert saved["indexed"] == 1
            updated = copy.deepcopy(config)
            updated["profile"]["revision"] = "test-new-revision"
            source = app.collections.instructions_root
            unavailable = tmp_path / "unavailable-instructions"
            source.rename(unavailable)
            try:
                with pytest.raises(ValueError, match="document directory is unavailable"):
                    app.apply_profile(updated, default_chunking())
            finally:
                unavailable.rename(source)
            result = app.apply_profile(updated, default_chunking())
            assert result["status"] == "ok"
            assert result["reembedded"]
            rows = app.index.read_rows(app.index._generic_table())
            assert len(rows) == 1 and rows[0]["collection"] == "instructions"
            assert rows[0]["text"] == "Preserve user boundaries."
            assert app.instructions.read("rules")["revision"] == saved["revision"]
