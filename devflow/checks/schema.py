"""The `devflow-checks` block the planner writes: a JSON array of HTTP checks.

SCHEMA is a JSON Schema (draft 2020-12) document, kept here as the contract
between prompts/plan.md and verify.py. `validate` implements the part of JSON
Schema it uses (type, required, properties, additionalProperties, enum, items,
minItems, pattern), so devflow needs no schema library.
"""

from __future__ import annotations

import re
from typing import Any


class BadChecks(ValueError):
    """The spec's checks are malformed, or would leave the task's own vhost (PRD SR7)."""


METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "devflow-checks",
    "type": "array",
    "items": {
        "type": "object",
        "required": ["path"],
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "pattern": "^/(?!/)"},
            "method": {"type": "string", "enum": METHODS},
            "expect_status": {"type": ["integer", "null"]},
            "contains": {"type": "string"},
            "absent": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "data": {"type": ["object", "null"]},
        },
    },
}

_TYPES: dict[str, Any] = {"array": list, "object": dict, "string": str, "integer": int, "null": type(None)}


def _check(value: Any, schema: dict[str, Any], where: str) -> None:
    kinds = schema.get("type")
    if kinds is not None:
        names = kinds if isinstance(kinds, list) else [kinds]
        if not any(isinstance(value, _TYPES[k]) and not (k == "integer" and isinstance(value, bool)) for k in names):
            raise BadChecks(f"{where} must be {' or '.join(names)}, not {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise BadChecks(f"{where} must be one of {', '.join(schema['enum'])}, not {value!r}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        raise BadChecks(f"{where} {value!r} doesn't match {schema['pattern']}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{where}[{i}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise BadChecks(f"{where} needs {key!r}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _check(item, props[key], f"{where}.{key}")
            elif extra is False:
                raise BadChecks(f"{where} has an unknown field {key!r} (allowed: {', '.join(props)})")
            elif isinstance(extra, dict):
                _check(item, extra, f"{where}.{key}")


def validate(data: Any) -> None:
    _check(data, SCHEMA, "checks")
