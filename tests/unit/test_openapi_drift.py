"""OpenAPI drift test — hand-authored vs FastAPI-generated.

Per ADR-0003 the hand-authored ``openapi/openapi.yaml`` is the
contract. FastAPI generates its own spec from routes + Pydantic
models. The two MUST agree on:

* Every path declared in the hand-spec is registered in the app.
* Each operation's HTTP methods + ``operationId`` + declared status
  codes match.
* Schemas that exist in both specs match field-for-field.

What's intentionally tolerated:

* ``info.version`` (auto-bumped by pyproject + ``__version__``).
* ``servers`` (env-specific).
* ``x-*`` vendor extensions auto-added by FastAPI.
* Schemas declared **only** in the hand-spec (Phase 5+ types like
  ``SearchResponse``, ``ContextPackResponse``, ``EvidenceChunk``).
  Phase 5 implementation lands the Pydantic models; until then the
  hand-spec is allowed to be ahead.

This test is the gate the design doc (docs/phase-5-design.md)
describes; it can land before Phase 5 implementation so the
contract is enforceable incrementally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.retrieval.types import (
    Conflict,
    ContextPackRequest,
    ContextPackResponse,
    EvidenceChunk,
    RelatedSource,
    RetrievalFilters,
    TokenBudget,
    Warning,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HAND_SPEC_PATH = REPO_ROOT / "openapi" / "openapi.yaml"

# Schemas that the hand-spec declares but Phase 5 will introduce as
# Pydantic models. Until that lands, the drift test ignores them.
PHASE_5_ONLY_SCHEMAS: frozenset[str] = frozenset(
    {
        "SearchRequest",
        "SearchResponse",
        "RetrievalFilters",
        "ResultCard",
        "Warning",
        "ContextPackRequest",
        "ContextPackResponse",
        "EvidenceChunk",
        "Conflict",
        "RelatedSource",
        "TokenBudget",
    }
)


@pytest.fixture(scope="module")
def hand_spec() -> dict[str, Any]:
    return yaml.safe_load(HAND_SPEC_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def app_spec() -> dict[str, Any]:
    with TestClient(create_app()) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


class TestPathsExist:
    """Every hand-spec path must exist in the FastAPI app."""

    def test_all_hand_paths_registered(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        missing = set(hand_spec["paths"]) - set(app_spec["paths"])
        assert not missing, (
            f"hand-authored OpenAPI declares paths the app doesn't serve: "
            f"{sorted(missing)}. Add a route or remove from the spec."
        )

    def test_no_undocumented_v1_paths_in_app(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        """App-side /v1 paths must be in the contract.

        We don't constrain non-/v1 paths (e.g., FastAPI's auto /openapi.json,
        /docs, /redoc) — those are operational, not part of the contract.
        """
        app_v1 = {p for p in app_spec["paths"] if p.startswith("/v1")}
        hand_v1 = {p for p in hand_spec["paths"] if p.startswith("/v1")}
        extras = app_v1 - hand_v1
        assert not extras, (
            f"app serves /v1 paths the OpenAPI contract doesn't declare: "
            f"{sorted(extras)}. Add to openapi.yaml or remove from the app."
        )


class TestOperationsAgree:
    """For shared paths, methods + operationIds + status codes match."""

    def _shared_paths(self, hand_spec: dict[str, Any], app_spec: dict[str, Any]) -> list[str]:
        return sorted(set(hand_spec["paths"]) & set(app_spec["paths"]))

    def test_methods_match(self, hand_spec: dict[str, Any], app_spec: dict[str, Any]) -> None:
        for path in self._shared_paths(hand_spec, app_spec):
            hand_methods = {
                m
                for m in hand_spec["paths"][path]
                if m in {"get", "post", "put", "delete", "patch"}
            }
            app_methods = {
                m for m in app_spec["paths"][path] if m in {"get", "post", "put", "delete", "patch"}
            }
            assert hand_methods == app_methods, (
                f"{path}: methods drift. hand={hand_methods}, app={app_methods}"
            )

    def test_operation_ids_match(self, hand_spec: dict[str, Any], app_spec: dict[str, Any]) -> None:
        for path in self._shared_paths(hand_spec, app_spec):
            for method in ("get", "post", "put", "delete", "patch"):
                if method not in hand_spec["paths"][path]:
                    continue
                hand_id = hand_spec["paths"][path][method].get("operationId")
                app_id = app_spec["paths"][path][method].get("operationId")
                assert hand_id == app_id, (
                    f"{method.upper()} {path}: operationId drift. hand={hand_id!r}, app={app_id!r}"
                )

    def test_declared_status_codes_match(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        """Each method's response keys (status codes) must be the same set.

        FastAPI auto-adds a ``422`` for routes with a request body — the
        contract should NOT declare 422 unless it's a real custom 422.
        We tolerate FastAPI's auto-422 to avoid forcing every contract
        to list it explicitly.
        """
        for path in self._shared_paths(hand_spec, app_spec):
            for method in ("get", "post", "put", "delete", "patch"):
                if method not in hand_spec["paths"][path]:
                    continue
                hand_codes = set(hand_spec["paths"][path][method].get("responses", {}))
                app_codes = set(app_spec["paths"][path][method].get("responses", {}))
                app_codes.discard("422")  # FastAPI auto-adds for body routes
                assert hand_codes == app_codes, (
                    f"{method.upper()} {path}: response status codes drift. "
                    f"hand={sorted(hand_codes)}, app={sorted(app_codes)}"
                )


class TestSchemasAgree:
    """Schemas that exist in both specs must have matching field sets."""

    def test_required_fields_match_for_shared_schemas(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        hand_schemas = hand_spec.get("components", {}).get("schemas", {})
        app_schemas = app_spec.get("components", {}).get("schemas", {})
        for name in sorted(set(hand_schemas) & set(app_schemas)):
            if name in PHASE_5_ONLY_SCHEMAS:
                continue
            hand_required = set(hand_schemas[name].get("required", []))
            app_required = set(app_schemas[name].get("required", []))
            assert hand_required == app_required, (
                f"schema {name}: required-field drift. "
                f"hand={sorted(hand_required)}, app={sorted(app_required)}"
            )

    def test_property_names_match_for_shared_schemas(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        hand_schemas = hand_spec.get("components", {}).get("schemas", {})
        app_schemas = app_spec.get("components", {}).get("schemas", {})
        for name in sorted(set(hand_schemas) & set(app_schemas)):
            if name in PHASE_5_ONLY_SCHEMAS:
                continue
            hand_props = set(hand_schemas[name].get("properties", {}))
            app_props = set(app_schemas[name].get("properties", {}))
            assert hand_props == app_props, (
                f"schema {name}: property-name drift. "
                f"hand-only={sorted(hand_props - app_props)}, "
                f"app-only={sorted(app_props - hand_props)}"
            )

    def test_enum_values_match_for_shared_schema_properties(
        self, hand_spec: dict[str, Any], app_spec: dict[str, Any]
    ) -> None:
        """Catch enum drift — the kind of bug a future Literal-type change
        introduces if the contract isn't kept in sync.

        Pydantic emits a single-value Literal as ``const: "X"`` and a
        multi-value Literal as ``enum: ["X", "Y"]``. Both are valid
        JSON Schema; this test normalizes ``const: X`` into
        ``enum: [X]`` before comparing.
        """
        hand_schemas = hand_spec.get("components", {}).get("schemas", {})
        app_schemas = app_spec.get("components", {}).get("schemas", {})
        for name in sorted(set(hand_schemas) & set(app_schemas)):
            if name in PHASE_5_ONLY_SCHEMAS:
                continue
            hand_props = hand_schemas[name].get("properties", {})
            app_props = app_schemas[name].get("properties", {})
            for prop in sorted(set(hand_props) & set(app_props)):
                hand_enum = _enum_or_const(hand_props[prop])
                app_enum = _enum_or_const(app_props[prop])
                if hand_enum is None and app_enum is None:
                    continue
                assert hand_enum is not None and app_enum is not None, (
                    f"schema {name}.{prop}: one side constrains values, the other doesn't. "
                    f"hand={hand_enum}, app={app_enum}"
                )
                assert set(hand_enum) == set(app_enum), (
                    f"schema {name}.{prop}: enum drift. "
                    f"hand-only={sorted(set(hand_enum) - set(app_enum))}, "
                    f"app-only={sorted(set(app_enum) - set(hand_enum))}"
                )


def _enum_or_const(schema: dict[str, Any]) -> list[Any] | None:
    """Return the value set a property is constrained to, or None.

    Treats JSON Schema ``const: X`` as equivalent to ``enum: [X]``; both
    say "this property must be exactly X". Pydantic emits ``const`` for
    single-value Literals and ``enum`` for multi-value ones.
    """
    if "enum" in schema:
        result = schema["enum"]
        return list(result) if isinstance(result, list) else [result]
    if "const" in schema:
        return [schema["const"]]
    return None


# ─── Direct Pydantic-vs-hand-spec checks (slice 3+) ──────────────────
#
# The schema-drift tests above only cross-check schemas that exist in
# BOTH the hand-spec and FastAPI's generated /openapi.json. A Pydantic
# model that isn't yet wired to a route falls out of that intersection
# and the hand-spec drifts uncaught. This block closes that gap by
# checking the Pydantic models directly via ``model_json_schema()``.

_PYDANTIC_TO_HAND_SCHEMA: list[tuple[str, type]] = [
    ("RetrievalFilters", RetrievalFilters),
    ("Warning", Warning),
    ("Conflict", Conflict),
    ("ContextPackRequest", ContextPackRequest),
    ("ContextPackResponse", ContextPackResponse),
    ("EvidenceChunk", EvidenceChunk),
    ("RelatedSource", RelatedSource),
    ("TokenBudget", TokenBudget),
]


class TestPydanticModelsMatchHandSpec:
    """Pydantic models in retrieval/types.py must agree with openapi.yaml.

    Runs against the in-process model schema, so it catches drift even
    when the model isn't yet wired to a FastAPI route. Each model is
    checked for the same required-fields + property-name sets as the
    hand-spec schema with the same name.
    """

    @pytest.mark.parametrize("schema_name,model_cls", _PYDANTIC_TO_HAND_SCHEMA)
    def test_required_fields_match(
        self, schema_name: str, model_cls: type, hand_spec: dict[str, Any]
    ) -> None:
        hand_schema = hand_spec["components"]["schemas"][schema_name]
        hand_required = set(hand_schema.get("required", []))
        pydantic_required = set(model_cls.model_json_schema().get("required", []))
        assert hand_required == pydantic_required, (
            f"{schema_name}: required-field drift between Pydantic and hand-spec. "
            f"hand-only={sorted(hand_required - pydantic_required)}, "
            f"pydantic-only={sorted(pydantic_required - hand_required)}"
        )

    @pytest.mark.parametrize("schema_name,model_cls", _PYDANTIC_TO_HAND_SCHEMA)
    def test_property_names_match(
        self, schema_name: str, model_cls: type, hand_spec: dict[str, Any]
    ) -> None:
        hand_schema = hand_spec["components"]["schemas"][schema_name]
        hand_props = set(hand_schema.get("properties", {}))
        pydantic_props = set(model_cls.model_json_schema().get("properties", {}))
        assert hand_props == pydantic_props, (
            f"{schema_name}: property-name drift between Pydantic and hand-spec. "
            f"hand-only={sorted(hand_props - pydantic_props)}, "
            f"pydantic-only={sorted(pydantic_props - hand_props)}"
        )
