"""Structural tests for OpenAPI security + ErrorResponse wiring (#294).

Two HIGH audit findings — landed together because they share the
spec file and the same review surface:

* OpenAPI must declare a bearer-auth security scheme so codegen
  tools (openapi-generator, openapi-python-client) produce a typed
  client with auth wiring. Without this declaration, agent
  consumers have to hand-patch every request.
* Every documented error response on protected endpoints must
  carry the ``ErrorResponse`` schema as its content type — not
  the bare ``description:`` text the pre-#294 spec used. A typed
  consumer that does codegen sees ``429 → unknown shape`` today.

These tests load ``openapi/openapi.yaml`` directly (the hand spec —
ADR-0003 says this is the source of truth) and pin the contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
HAND_SPEC_PATH = REPO_ROOT / "openapi" / "openapi.yaml"

# Endpoints that protected-by-default; they SHOULD inherit the
# doc-level security and have ErrorResponse wired to error statuses.
_PROTECTED_OPERATIONS: tuple[str, ...] = (
    "/v1/search",
    "/v1/answer",
    "/v1/agent/context-pack",
)

# Probes that explicitly opt OUT of auth via `security: []`.
_PUBLIC_OPERATIONS: tuple[str, ...] = (
    "/healthz",
    "/readyz",
    "/version",
)

# Error statuses that should carry ErrorResponse content schema.
_ERROR_STATUSES: tuple[str, ...] = ("401", "422", "429", "500", "503")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load(HAND_SPEC_PATH.read_text(encoding="utf-8"))


class TestSecurityScheme:
    """Bearer-auth declaration + per-operation security opts (#294)."""

    def test_bearer_auth_security_scheme_declared(self, spec: dict[str, Any]) -> None:
        """The components.securitySchemes block MUST declare
        bearerAuth as HTTP bearer. Without this declaration, a
        codegen client has no auth machinery to use."""
        schemes = spec.get("components", {}).get("securitySchemes", {})
        assert "bearerAuth" in schemes, (
            "components.securitySchemes.bearerAuth missing — typed clients "
            "have no way to wire Authorization headers"
        )
        bearer = schemes["bearerAuth"]
        assert bearer.get("type") == "http"
        assert bearer.get("scheme") == "bearer"

    def test_doc_level_security_requires_bearer(self, spec: dict[str, Any]) -> None:
        """The top-level `security:` block applies bearer as the
        default for every operation. Operations opt out by setting
        `security: []`.

        #315: the block also lists oauth2 (OIDC) — a route is satisfied
        by EITHER scheme. The middleware accepts whichever
        KILN_AUTH_MODE wires up.
        """
        sec = spec.get("security", [])
        # bearer must be present + first (highest-priority scheme).
        assert sec, f"expected at least one doc-level security scheme, got {sec!r}"
        assert {"bearerAuth": []} in sec, (
            f"expected bearerAuth in doc-level security, got {sec!r}"
        )

    @pytest.mark.parametrize("path", _PUBLIC_OPERATIONS)
    def test_public_operations_opt_out_of_security(self, spec: dict[str, Any], path: str) -> None:
        """Probes (/healthz, /readyz, /version) MUST opt out via
        `security: []` so they remain reachable when bearer auth is
        enforced. Matches the public-path allowlist in api/auth.py."""
        # All three are GET-only.
        op = spec["paths"][path]["get"]
        assert "security" in op, f"{path} does not opt out of bearer auth; add `security: []`"
        assert op["security"] == [], (
            f"{path} security block must be the empty list; got {op['security']!r}"
        )


class TestErrorResponseWiring:
    """Every documented error response on protected endpoints must
    carry the ErrorResponse schema as content (#294)."""

    @pytest.mark.parametrize("path", _PROTECTED_OPERATIONS)
    def test_protected_operations_inherit_security(self, spec: dict[str, Any], path: str) -> None:
        """Protected operations do NOT carry an explicit `security:`
        block — they inherit the doc-level requirement. If a future
        edit adds `security: []` to one of them, that's a sec
        regression and this test trips."""
        op = spec["paths"][path]["post"]
        assert "security" not in op or op["security"] != [], (
            f"{path} must inherit doc-level bearerAuth requirement"
        )

    @pytest.mark.parametrize("path", _PROTECTED_OPERATIONS)
    def test_protected_operations_document_all_error_statuses(
        self, spec: dict[str, Any], path: str
    ) -> None:
        """Every protected operation must document 401 / 422 / 429
        / 500 at minimum. Without this, a typed client has no way
        to model the error envelope shape for these statuses."""
        op = spec["paths"][path]["post"]
        responses = set(op["responses"].keys())
        required = {"401", "422", "429", "500"}
        missing = required - responses
        assert not missing, (
            f"{path} is missing error responses {sorted(missing)}; "
            "every protected endpoint must document the closed-set "
            "of statuses agents need to handle"
        )

    @pytest.mark.parametrize("path", _PROTECTED_OPERATIONS)
    @pytest.mark.parametrize("status", _ERROR_STATUSES)
    def test_error_responses_carry_error_response_schema(
        self, spec: dict[str, Any], path: str, status: str
    ) -> None:
        """For each documented error status on a protected endpoint,
        the application/json content schema MUST reference
        ErrorResponse — either directly or via a $ref to a reusable
        response in components.responses. Pre-#294 the spec had a
        bare `description:` text with no schema."""
        op = spec["paths"][path]["post"]
        if status not in op["responses"]:
            pytest.skip(f"{path} doesn't document {status}")
        response = op["responses"][status]
        # Either a $ref to components.responses (preferred) OR an
        # inline `content.application/json.schema` $ref to
        # ErrorResponse — both are valid wirings.
        if "$ref" in response:
            assert response["$ref"].startswith("#/components/responses/"), (
                f"{path} {status} $ref must point at components.responses"
            )
        else:
            content = response.get("content", {}).get("application/json", {})
            schema = content.get("schema", {})
            ref = schema.get("$ref", "")
            assert ref == "#/components/schemas/ErrorResponse", (
                f"{path} {status} content schema must $ref ErrorResponse; got {ref!r}"
            )


class TestReusableErrorResponses:
    """The components.responses block defines reusable error shapes
    that operations $ref. Each must carry an example so typed
    clients have a concrete payload to model against (#294)."""

    @pytest.mark.parametrize("name", ["Unauthorized", "UnprocessableEntity", "InternalError"])
    def test_response_defined(self, spec: dict[str, Any], name: str) -> None:
        responses = spec.get("components", {}).get("responses", {})
        assert name in responses, f"components.responses.{name} missing — operations $ref it"

    @pytest.mark.parametrize("name", ["Unauthorized", "UnprocessableEntity", "InternalError"])
    def test_response_schema_is_error_response(self, spec: dict[str, Any], name: str) -> None:
        """The reusable response MUST point its application/json
        content at the ErrorResponse schema."""
        responses = spec["components"]["responses"]
        content = responses[name].get("content", {}).get("application/json", {})
        ref = content.get("schema", {}).get("$ref", "")
        assert ref == "#/components/schemas/ErrorResponse"

    @pytest.mark.parametrize("name", ["Unauthorized", "UnprocessableEntity", "InternalError"])
    def test_response_carries_example(self, spec: dict[str, Any], name: str) -> None:
        """Every reusable error response must carry at least one
        `examples:` entry so typed clients have a concrete payload
        to model against (the error_code, retry_safe shape, and
        detail structure)."""
        responses = spec["components"]["responses"]
        content = responses[name].get("content", {}).get("application/json", {})
        examples = content.get("examples", {})
        assert examples, f"{name} response carries no `examples:` block"
        # Each example MUST $ref a components.examples entry — the
        # examples are reusable too.
        for ex_name, ex in examples.items():
            assert "$ref" in ex, f"{name}.examples.{ex_name} must $ref components.examples"

    def test_error_examples_carry_required_envelope_fields(self, spec: dict[str, Any]) -> None:
        """Each error example's value MUST carry the three required
        fields from ErrorResponse: error_code, message, retry_safe.
        Without this, a codegen consumer that pattern-matches the
        example shape would miss the required envelope keys."""
        examples = spec.get("components", {}).get("examples", {})
        assert examples, "components.examples block missing"
        required = {"error_code", "message", "retry_safe"}
        for name, ex in examples.items():
            value = ex.get("value", {})
            missing = required - set(value.keys())
            assert not missing, (
                f"components.examples.{name} value missing required envelope "
                f"fields: {sorted(missing)}"
            )
            # error_code must be a member of the closed-set enum.
            enum = spec["components"]["schemas"]["ErrorResponse"]["properties"]["error_code"][
                "enum"
            ]
            assert value["error_code"] in enum, (
                f"{name} error_code {value['error_code']!r} not in enum {enum!r}"
            )
