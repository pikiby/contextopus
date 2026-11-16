"""Shared instruction bodies remain authoritative when derived storage fails."""

import hashlib
from unittest.mock import patch

import pytest

from contextopus.core.documents.instructions import InstructionLibrary
from contextopus.core.documents import DocumentMutations
from contextopus.core.embeddings import default_config
from contextopus.core.rag import DocumentKey, IndexDocument, RagIndex
from contextopus.core.resources import ProcessResources
from contextopus.core.storage import StorageLayout
from contextopus.startup import Application
from fake_embeddings import Factory


def metadata(ident, **values):
    return {"id": ident, "type": "instruction", "title": ident,
            "description": "Instructions for " + ident, **values}


def save(library, path, values, body, revision=None):
    mutations = DocumentMutations(library.collections)
    preview = mutations.preview("instructions", path, values, body, expected_revision=revision)
    return mutations.save("instructions", path, values, body, expected_revision=revision,
                          approval={"change_revision": preview["change_revision"], "confirmation": "Test approval"})


def test_missing_reads_do_not_initialize_any_storage(tmp_path):
    root = tmp_path / "absent"
    library = InstructionLibrary(StorageLayout(root))
    assert library.list() == {"instructions": []}
    assert library.resolve([])["instructions"] == []
    with pytest.raises(FileNotFoundError):
        library.read("[[missing]]")
    assert not root.exists()


def test_lossless_body_full_file_revision_and_yaml_conflicts(tmp_path):
    library = InstructionLibrary(StorageLayout(tmp_path))
    body = "---\nuser: front matter\n---\n\nРусский текст  \n[[ordinary]]\n"
    saved = save(library, "workspace.project.md", metadata("stable", config={"model": "chosen"}), body)
    assert saved["content"] == body
    assert saved["index_pending"] is False
    path = library.root / saved["path"]
    assert saved["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert library.read("[[workspace.project|Project]]")["content"] == body
    assert library.read("stable")["metadata"]["config"] == {"model": "chosen"}
    listing = library.list()["instructions"][0]
    assert listing["content"] == body and listing["metadata"]["id"] == "stable"
    with pytest.raises(ValueError, match="revision|changed"):
        save(library, saved["path"], saved["metadata"], "overwrite")
    changed = path.read_text(encoding="utf-8").replace("model: chosen", "model: externally-changed")
    path.write_text(changed, encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="revision|changed"):
        save(library, saved["path"], saved["metadata"], "overwrite", saved["revision"])
    assert path.read_text(encoding="utf-8") == changed
    current = library.read("stable")
    updated = save(library, current["path"], current["metadata"], body + "Updated\n", current["revision"])
    assert updated["revision"] != current["revision"]


def test_exact_dependency_resolution_deduplicates_and_tracks_metadata(tmp_path):
    library = InstructionLibrary(StorageLayout(tmp_path))
    common = save(library, "common.md", metadata("common"), "Common body")
    save(library, "first.md", metadata("first", requires_rule=["[[common]]"]), "First [[not-required]]")
    save(library, "second.md", metadata("second", requires_rule=["[[common]]"]), "Second")
    result = library.resolve(["first", "[[second|Label]]", "first"])
    assert [item["id"] for item in result["instructions"]] == ["common", "first", "second"]
    save(library, "common.md", metadata("common", config={"changed": True}), "Common body", common["revision"])
    assert library.resolve(["first", "second"])["revision"] != result["revision"]
    first = library.read("first")
    save(library, "first.md", metadata("first", requires_rule=["[[first]]"]), "body", first["revision"])
    with pytest.raises(ValueError, match="Cyclic"):
        library.resolve(["first"])


def test_ambiguity_duplicate_identity_and_invalid_dependency_fail_closed(tmp_path):
    library = InstructionLibrary(StorageLayout(tmp_path))
    save(library, "a/rules.md", metadata("first"), "one")
    save(library, "b/rules.md", metadata("second"), "two")
    with pytest.raises(ValueError, match="Ambiguous"):
        library.read("[[rules]]")
    assert library.read("a/rules.md")["id"] == "first"
    with pytest.raises(ValueError, match="Duplicate"):
        save(library, "another.md", metadata("first"), "duplicate")
    save(library, "missing.md", metadata("missing", requires_rule=["[[absent]]"]), "body")
    with pytest.raises(ValueError, match="Missing"):
        library.resolve(["missing"])


@pytest.mark.parametrize("path", ["../escape.md", "/escape.md", "nested/../../escape.md", ".hidden.md", "rules.txt", "a\\b.md", "C:escape.md"])
def test_path_rejection_precedes_writes(tmp_path, path):
    root = tmp_path / "absent"
    library = InstructionLibrary(StorageLayout(root))
    with pytest.raises(ValueError):
        save(library, path, metadata("id"), "body")
    assert not root.exists()


def test_symlinks_and_invalid_source_yaml_are_errors(tmp_path):
    library = InstructionLibrary(StorageLayout(tmp_path / "root"))
    save(library, "a.md", metadata("a"), "body")
    (library.root / "alias.md").symlink_to(library.root / "a.md")
    with pytest.raises(ValueError, match="symlink"):
        library.read("a")
    (library.root / "alias.md").unlink()
    (library.root / "invalid.md").write_text("---\nid: invalid\nid: duplicate\ntype: instruction\n---\nbody")
    with pytest.raises(ValueError, match="unique"):
        library.list()


def test_reconcile_only_changes_instruction_index(tmp_path):
    factory = Factory()
    config = default_config()
    config["profile"]["dimensions"] = 2
    with ProcessResources(factory) as resources:
        index = RagIndex(StorageLayout(tmp_path), resources, embeddings=config)
        library = InstructionLibrary(index.layout, index)
        unrelated = IndexDocument(DocumentKey("core", "search_algorithm", "algorithm"),
                                  "revision", "Algorithm", "algorithm.md", {"type": "algorithm"})
        index.index(unrelated)
        library.collections.reconcile("instructions")
        assert index.read_rows(index.table, predicate=index.key_predicate(unrelated.key))


def test_invalid_nested_yaml_metadata_rejected_before_source_creation(tmp_path):
    root = tmp_path / "absent"
    library = InstructionLibrary(StorageLayout(root))
    with pytest.raises(ValueError, match="unique strings"):
        save(library, "bad.md", metadata("bad", config={1: "numeric key"}), "body")
    assert not root.exists()


def test_failed_index_save_is_readable_and_explicit_reconcile_repairs(tmp_path):
    factory = Factory()
    config = default_config()
    config["profile"]["dimensions"] = 2
    with ProcessResources(factory) as resources:
        index = RagIndex(StorageLayout(tmp_path), resources, embeddings=config)
        library = InstructionLibrary(index.layout, index)
        with patch.object(index, "index", side_effect=RuntimeError("embedding unavailable")):
            saved = save(library, "a.md", metadata("a"), "Complete authoritative body")
        assert saved["index_pending"] is True
        assert saved["index_error"] == "embedding unavailable"
        assert library.read("a")["content"] == "Complete authoritative body"
        assert library.collections.reconcile("instructions")["indexed"] == 1
        calls = len(factory.calls)
        path = library.root / "a.md"
        path.write_text(path.read_text(encoding="utf-8").replace("title: a", "title: New title"))
        assert library.collections.reconcile("instructions")["refreshed"] == 1
        assert len(factory.calls) == calls
        selection = {"algorithm": "none", "parameters": {"page_size": 20}}
        assert index.search_detailed("authoritative", module="core", collection="instructions", selection=selection)["results"]
        path.write_text(path.read_text(encoding="utf-8").replace("Complete authoritative body", "New source body"))
        assert index.search_detailed("authoritative", module="core", collection="instructions", selection=selection)["results"] == []
        assert library.collections.reconcile("instructions")["indexed"] == 1
        unrelated = IndexDocument(DocumentKey("core", "search_algorithm", "algorithm"),
                                  "revision", "Algorithm", "algorithm.md", {"type": "algorithm"})
        index.index(unrelated)
        assert index.read_rows(index.table, predicate=index.key_predicate(unrelated.key))
        path.unlink()
        result = index.search_detailed("authoritative", module="core", collection="instructions", selection=selection)
        assert result["results"] == []
        assert library.collections.reconcile("instructions")["deleted"] == 1
        assert index.read_rows(index.table, predicate=index.key_predicate(unrelated.key))


def test_core_registry_exposes_document_operations_without_models(tmp_path):
    def forbidden(config):
        raise AssertionError("Instruction file operation loaded an embedding model")

    with ProcessResources(forbidden) as resources:
        with Application(tmp_path / "root", resources=resources, discover=False) as app:
            operations = app.query("describe")["operations"]
            assert "core.library" in operations["query"]
            assert "core.documents_save" in operations["execute"]
            args = {"collection": "instructions", "path": "main.md", "metadata": metadata("main"), "content": "Main"}
            preview = app.query("core.documents_preview", args)
            saved = app.execute("core.documents_save", {**args, "approval": {
                "change_revision": preview["change_revision"], "confirmation": "Test approval"}})
            assert saved["saved"]
            assert not resources.providers._entries
            assert saved["index_pending"] is True
            assert "loaded an embedding model" in saved["index_error"]
            assert app.query("core.library", {"action": "record", "collection": "instructions", "id": "main"})["content"] == "Main"
            assert app.instructions.resolve(["main"])["instructions"][0]["id"] == "main"
