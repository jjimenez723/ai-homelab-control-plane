"""A small, pure-Python validator for the subset of JSON Schema 2020-12 that
`templates/app-spec/schema-v1.json` actually uses.

`jsonschema` is not a declared dependency anywhere in this repository
(`requirements-ingest.txt`, `requirements-social-video.txt`) and CI
(`.github/workflows/ci.yml`) installs only those two files, so importing it
unconditionally would work on a developer machine that happens to have it
installed (as a transitive dependency of something else entirely -- it is
present in this repository's `.venv` only because `altair` pulls it in) and
then fail in CI. Rather than adding a new pinned dependency for one schema,
this module implements exactly the keywords the app-spec schema uses:
``type``, ``required``, ``enum``, ``additionalProperties``, ``properties``,
``items``, ``minItems``, ``pattern``, and ``$ref`` to a local ``$defs``
entry. Nothing else in JSON Schema is supported, and that is deliberate --
see `tools/appspec/schema.py` for the one schema this validates against.

This is not a general JSON Schema implementation. It does not support
``oneOf``/``anyOf``/``allOf``/``not``, ``$ref`` outside the current
document, ``patternProperties``, numeric bounds, string length bounds,
``const``, tuple-form ``items``, or schema composition. Reaching for any of
those in `schema-v1.json` requires extending this module first, on purpose:
the schema's keyword surface should stay small enough that one file can
validate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")


@dataclass(frozen=True)
class SchemaIssue:
    """One structural validation failure."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class UnsupportedSchemaKeyword(ValueError):
    """A schema uses a keyword this validator does not implement."""


def _check_supported(schema: dict) -> None:
    supported = {
        "type", "required", "enum", "additionalProperties", "properties",
        "items", "minItems", "pattern", "$ref", "$defs", "$schema", "$id",
        "title", "description",
    }
    unknown = set(schema.keys()) - supported
    if unknown:
        raise UnsupportedSchemaKeyword(
            f"schema uses unsupported keyword(s) {sorted(unknown)}; "
            "extend tools/appspec/jsonschema_subset.py or avoid them in the schema"
        )


def _type_matches(instance: Any, types: list[str]) -> bool:
    for t in types:
        if t == "object" and isinstance(instance, dict):
            return True
        if t == "array" and isinstance(instance, list):
            return True
        if t == "string" and isinstance(instance, str):
            return True
        if t == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if t == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if t == "boolean" and isinstance(instance, bool):
            return True
        if t == "null" and instance is None:
            return True
    return False


def _resolve_ref(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise UnsupportedSchemaKeyword(
            f"$ref {ref!r} is not a local in-document reference (must start with '#/')"
        )
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise UnsupportedSchemaKeyword(f"$ref {ref!r} does not resolve within the document")
        node = node[part]
    return node


def _check(instance: Any, schema: dict, root: dict, path: str, issues: list[SchemaIssue]) -> None:
    _check_supported(schema)

    if "$ref" in schema:
        _check(instance, _resolve_ref(root, schema["$ref"]), root, path, issues)
        return

    if "type" in schema:
        raw = schema["type"]
        types = raw if isinstance(raw, list) else [raw]
        for t in types:
            if t not in _JSON_TYPES:
                raise UnsupportedSchemaKeyword(f"unsupported type {t!r} at {path}")
        if not _type_matches(instance, types):
            issues.append(SchemaIssue(path, f"expected type {types}, got {type(instance).__name__}"))
            return

    if "enum" in schema and instance not in schema["enum"]:
        issues.append(SchemaIssue(path, f"{instance!r} is not one of {schema['enum']!r}"))

    if isinstance(instance, str) and "pattern" in schema:
        if re.search(schema["pattern"], instance) is None:
            issues.append(SchemaIssue(path, f"{instance!r} does not match pattern {schema['pattern']!r}"))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                issues.append(SchemaIssue(path, f"missing required property {key!r}"))
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child_path = f"{path}.{key}"
            if key in props:
                _check(value, props[key], root, child_path, issues)
            elif additional is False:
                issues.append(SchemaIssue(path, f"additional property {key!r} is not allowed"))
            elif isinstance(additional, dict):
                _check(value, additional, root, child_path, issues)

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            issues.append(SchemaIssue(path, f"expected at least {min_items} item(s), got {len(instance)}"))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(instance):
                _check(item, item_schema, root, f"{path}[{index}]", issues)


def validate(instance: Any, schema: dict) -> list[SchemaIssue]:
    """Validate ``instance`` against ``schema``, returning every issue found.

    ``schema`` is treated as its own document root, so a ``$ref`` of
    ``#/$defs/foo`` resolves against ``schema["$defs"]["foo"]``. An empty
    list means the instance is structurally valid under the supported
    keyword subset.
    """
    issues: list[SchemaIssue] = []
    _check(instance, schema, schema, "$", issues)
    return issues
