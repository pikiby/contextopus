"""Compose the Core guide() router from module declarations and Core overrides.

Modules declare routes and steps with defaults in their manifests. The root
settings field ``router`` stores only user overrides; without an override the
module default applies. Core owns the composition, so no module provides the
default procedure.
"""

import re

BASE_RULES = "base_rules.md"
DEFAULT_ORDER = 100
PLACES = ("guide",)

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ROUTE_OVERRIDES = {"enabled": bool, "order": int, "when": str}
_STEP_OVERRIDES = {"enabled": bool, "required": bool, "order": int, "text": str, "in": list}

CORE_ROUTES = (
    {"id": "work", "when": "Core workflow: enter context, read rules/required_reads, search, browse library, edit documents or settings",
     "procedure": "work"},
    {"id": "modules", "when": "Inspect, install, update or disable Contextopus modules",
     "procedure": "modules"},
    {"id": "embeddings", "when": "Configure the embedding model, chunking or index rebuild",
     "procedure": "embeddings"},
    {"id": "setup", "when": "Diagnose or repair the Contextopus service, model setup or client connection",
     "procedure": "setup"},
)


def declaration_errors(module, router):
    """Return manifest problems that the JSON schema cannot express."""
    errors = []
    for kind in ("routes", "steps"):
        seen = set()
        for entry in router.get(kind, []):
            if entry["id"] in seen:
                errors.append(f"Duplicate {kind[:-1]} id: {entry['id']}")
            seen.add(entry["id"])
            if not entry["procedure"].startswith(module + "/"):
                errors.append(f"{kind[:-1]} {entry['id']} must name a procedure of module {module}")
    return errors


def validate_overrides(value):
    """Validate the root ``router`` settings value: module -> routes/steps -> id -> fields."""
    if not isinstance(value, dict):
        raise ValueError("router must be an object")
    for module, sections in value.items():
        if not isinstance(module, str) or not _ID.match(module):
            raise ValueError("router keys must be module ids")
        if not isinstance(sections, dict) or set(sections) - {"routes", "steps"}:
            raise ValueError("router." + module + " accepts only routes and steps")
        for kind, allowed in (("routes", _ROUTE_OVERRIDES), ("steps", _STEP_OVERRIDES)):
            entries = sections.get(kind, {})
            if not isinstance(entries, dict):
                raise ValueError(f"router.{module}.{kind} must be an object")
            for ident, fields in entries.items():
                where = f"router.{module}.{kind}.{ident}"
                if not isinstance(ident, str) or not _ID.match(ident):
                    raise ValueError(where + ": invalid id")
                if not isinstance(fields, dict) or not fields or set(fields) - set(allowed):
                    raise ValueError(where + " accepts " + ", ".join(sorted(allowed)))
                for name, kind_type in allowed.items():
                    if name in fields and type(fields[name]) is not kind_type:
                        raise ValueError(f"{where}.{name} must be {kind_type.__name__}")
                if "text" in fields and not fields["text"].strip() or "when" in fields and not fields["when"].strip():
                    raise ValueError(where + ": text must not be empty")
                if "in" in fields and (not fields["in"] or len(set(fields["in"])) != len(fields["in"])
                                       or set(fields["in"]) - set(PLACES)):
                    raise ValueError(where + ".in must list only guide")


def entries(manifests, overrides):
    """Effective enabled routes and steps, with overridden fields named."""
    routes, steps = [], []
    declared = {"core": {"routes": list(CORE_ROUTES), "steps": []}}
    for module in sorted(manifests):
        router = manifests[module].get("router")
        if router:
            declared[module] = router
    for module, router in declared.items():
        changed = overrides.get(module, {})
        for kind, target, defaults in (
            ("routes", routes, {"enabled": True, "order": DEFAULT_ORDER}),
            ("steps", steps, {"enabled": True, "required": True, "order": DEFAULT_ORDER, "in": ["guide"]}),
        ):
            for entry in router.get(kind, []):
                value = {**defaults, **entry}
                override = changed.get(kind, {}).get(entry["id"], {})
                value.update(override)
                value["module"] = module
                value["overridden"] = sorted(override)
                if value["enabled"]:
                    target.append(value)
    key = lambda entry: (entry["order"], entry["module"], entry["id"])
    return {"routes": sorted(routes, key=key), "steps": sorted(steps, key=key)}


def _step_lines(steps):
    return [f"- {'Required' if step['required'] else 'Recommended'}: {step['text']} "
            f"Procedure: guide(procedure=\"{step['procedure']}\")." for step in steps]


def router_procedure(base, effective):
    """guide() without arguments: Core base rules, module routes and guide steps."""
    steps = [step for step in effective["steps"] if "guide" in step["in"]]
    lines = [base.rstrip(), "", "## Routes",
             "Choose the route that matches the task and obtain its procedure before acting."]
    lines += [f"- {route['when']}: guide(procedure=\"{route['procedure']}\")" for route in effective["routes"]]
    if steps:
        lines += ["", "## Steps for every task"] + _step_lines(steps)
    return {"name": "default", "content": "\n".join(lines) + "\n",
            "routes": [_public(route) for route in effective["routes"]],
            "steps": [_public(step) for step in steps]}


def attach(result, base, effective, name):
    """Wrap one procedure with optional active guide steps without duplicating base rules."""
    steps = [step for step in effective["steps"] if "guide" in step["in"] and step["procedure"] != name]
    parts = ["# Procedure: " + name, "", result["content"].rstrip()]
    if steps:
        parts += ["", "## Steps for every task"] + _step_lines(steps)
    return {**result, "content": "\n".join(parts) + "\n", "steps": [_public(step) for step in steps]}


def _public(entry):
    return {key: value for key, value in entry.items() if key not in {"enabled"}}
