#!/usr/bin/env python3
"""Minimal OpenAPI 3.1 validator.

Validates structure without external dependencies so `make openapi-lint`
works during bootstrap. Replace with `openapi-spec-validator` or
`spectral` once the dev toolchain settles. Exits non-zero on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def fail(msg: str) -> None:
    print(f"openapi-lint: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: lint_openapi.py <path-to-openapi.yaml>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        fail(f"file not found: {path}")
    try:
        spec: Any = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        fail(f"YAML parse error: {e}")

    if not isinstance(spec, dict):
        fail("top-level document must be a mapping")

    required_top = ("openapi", "info", "paths")
    for key in required_top:
        if key not in spec:
            fail(f"missing required top-level key: {key}")

    version = str(spec["openapi"])
    if not version.startswith(("3.0", "3.1")):
        fail(f"openapi version must be 3.0.x or 3.1.x, got {version!r}")

    info = spec["info"]
    for key in ("title", "version"):
        if key not in info:
            fail(f"info.{key} is required")

    paths = spec["paths"]
    if not isinstance(paths, dict) or not paths:
        fail("paths must be a non-empty mapping")

    for route, ops in paths.items():
        if not route.startswith("/"):
            fail(f"path {route!r} must start with '/'")
        if not isinstance(ops, dict):
            fail(f"path {route!r} must map to an operations object")
        for method, op in ops.items():
            if method in {"$ref", "summary", "description", "parameters", "servers"}:
                continue
            if method.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "trace",
            }:
                fail(f"unknown HTTP method {method!r} on path {route!r}")
            if not isinstance(op, dict):
                fail(f"operation {method.upper()} {route} must be an object")
            if "responses" not in op:
                fail(f"operation {method.upper()} {route} missing 'responses'")

    print(f"openapi-lint: OK — {len(paths)} paths, version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
