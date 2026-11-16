import pytest

from contextopus.core import router
from contextopus.startup import Application

MANIFESTS = {
    "notes": {"router": {
        "routes": [
            {"id": "work", "when": "Work with notes", "procedure": "notes/work", "order": 20},
            {"id": "hidden", "when": "Hidden route", "procedure": "notes/hidden", "enabled": False},
        ],
        "steps": [
            {"id": "card", "text": "Keep the note card current.", "procedure": "notes/card",
             "in": ["guide", "finish"]},
            {"id": "close", "text": "Check the note at the end.", "procedure": "notes/close", "in": ["finish"]},
        ],
    }},
    "plain": {},
}


def test_router_applies_module_defaults_and_core_overrides():
    effective = router.entries(MANIFESTS, {})
    assert [route["id"] for route in effective["routes"]] == ["work", "embeddings", "modules", "setup", "work"]
    assert [step["id"] for step in effective["steps"]] == ["card", "close"]
    response = router.router_procedure("# Base\n", effective)
    assert response["content"].startswith("# Base\n")
    assert 'Work with notes: guide(procedure="notes/work")' in response["content"]
    assert "Required: Keep the note card current." in response["content"]
    assert "Check the note at the end." not in response["content"]

    changed = router.entries(MANIFESTS, {"notes": {
        "routes": {"hidden": {"enabled": True, "order": 1}, "work": {"when": "Notes"}},
        "steps": {"card": {"required": False, "text": "Update the card.", "in": ["finish"]},
                  "close": {"in": ["guide"]}},
    }, "core": {"routes": {"modules": {"enabled": False}}}})
    assert [route["id"] for route in changed["routes"]] == ["hidden", "work", "embeddings", "setup", "work"]
    assert changed["routes"][1]["when"] == "Notes" and changed["routes"][1]["overridden"] == ["when"]
    guide_steps = router.router_procedure("# Base\n", changed)["steps"]
    assert [step["id"] for step in guide_steps] == ["close"]


def test_disabled_module_contributes_nothing():
    effective = router.entries({}, {"notes": {"routes": {"work": {"enabled": True}}}})
    assert [route["procedure"] for route in effective["routes"]] == ["embeddings", "modules", "setup", "work"]
    assert effective["steps"] == []


def test_attach_skips_duplicating_base_rules_and_skips_requested_step():
    effective = router.entries(MANIFESTS, {})
    attached = router.attach({"name": "notes/card", "content": "Card rules.\n"}, "# Base\n", effective, "notes/card")
    assert attached["content"].startswith("# Procedure: notes/card\n\nCard rules.\n")
    assert "# Base" not in attached["content"]
    assert attached["steps"] == []
    other = router.attach({"name": "notes/work", "content": "Work rules.\n"}, "# Base\n", effective, "notes/work")
    assert other["content"].endswith('Procedure: guide(procedure="notes/card").\n')


@pytest.mark.parametrize("value", [
    [], {"Bad": {}}, {"notes": {"other": {}}}, {"notes": {"routes": []}},
    {"notes": {"routes": {"work": {}}}}, {"notes": {"routes": {"work": {"text": "x"}}}},
    {"notes": {"steps": {"card": {"required": "yes"}}}}, {"notes": {"steps": {"card": {"in": []}}}},
    {"notes": {"steps": {"card": {"in": ["guide", "guide"]}}}}, {"notes": {"steps": {"card": {"in": ["exit"]}}}},
    {"notes": {"steps": {"card": {"text": " "}}}},
])
def test_invalid_router_overrides_are_rejected(value):
    with pytest.raises(ValueError):
        router.validate_overrides(value)


def test_declarations_must_name_own_unique_procedures():
    assert router.declaration_errors("notes", MANIFESTS["notes"]["router"]) == []
    errors = router.declaration_errors("notes", {"routes": [
        {"id": "a", "when": "A", "procedure": "other/a"},
        {"id": "a", "when": "B", "procedure": "notes/b"}]})
    assert any("must name a procedure of module notes" in error for error in errors)
    assert any("Duplicate route id: a" in error for error in errors)


def test_guide_router_uses_saved_root_overrides(tmp_path):
    with Application(tmp_path, discover=False) as app:
        default = app.procedure("default")
        assert default["name"] == "default"
        assert "Contextopus base rules" in default["content"]
        assert [route["procedure"] for route in default["routes"]] == ["embeddings", "modules", "setup", "work"]
        assert app.procedure("modules")["content"].startswith("# Procedure: modules")
        assert app.procedure("core/work")["status"] == "not_found"
        assert "core/work" not in [row["name"] for row in app.procedure("unknown")["procedures"]]
        preview = app.query("core.settings_preview", {"changes": {
            "router": {"core": {"routes": {"modules": {"enabled": False}}}}}})
        assert app.execute("core.settings_apply", {"preview_revision": preview["preview_revision"]})["saved"]
        assert [route["procedure"] for route in app.procedure("default")["routes"]] == ["embeddings", "setup", "work"]
        with pytest.raises(ValueError, match="root scope"):
            app.query("core.settings_preview", {"changes": {"router": {}}, "module": "notes"})
        with pytest.raises(ValueError, match="accepts"):
            app.query("core.settings_preview", {"changes": {"router": {"core": {"routes": {"modules": {"bad": 1}}}}}})


def test_modules_cannot_register_the_default_procedure(tmp_path):
    from contextopus.core.module_management.contributions import ContributionHost
    contribution = ContributionHost.__new__(ContributionHost)
    contribution.procedures = {}
    with pytest.raises(ValueError, match="Core guide\\(\\) router"):
        contribution.procedure("default", lambda: {})
