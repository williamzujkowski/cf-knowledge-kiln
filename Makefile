# cf-knowledge-kiln — local quality gate and CF lifecycle targets.
# `make verify` is the primary local gate. Everything else is plumbing.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PY        ?= python3
UV        ?= uv
PIP       ?= $(PY) -m pip
PYTEST    ?= $(PY) -m pytest
RUFF      ?= $(PY) -m ruff
MYPY      ?= $(PY) -m mypy

SRC_DIR   := src
TESTS_DIR := tests
PKG       := cf_knowledge_kiln

.DEFAULT_GOAL := help

.PHONY: help bootstrap install lock lint format typecheck test test-unit test-integration \
        eval security sbom scan openapi-lint run run-worker migrate migrate-down \
        ingest reembed reembed-dry-run cf-push cf-verify verify clean build-css verify-css

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*?##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap: ## Install dev dependencies (uses uv if available, else pip).
	@command -v $(UV) >/dev/null 2>&1 && $(UV) pip install --system -e ".[dev]" || $(PIP) install -e ".[dev]"
	@command -v pre-commit >/dev/null 2>&1 && pre-commit install || echo "pre-commit not installed; skipping hook install"

install: ## Install the package in editable mode.
	@$(PIP) install -e .

lock: ## Regenerate uv.lock against the current pyproject.toml.
	@command -v $(UV) >/dev/null 2>&1 || { echo "uv is required to refresh the lockfile — install from https://docs.astral.sh/uv/" >&2; exit 1; }
	@$(UV) lock

lint: ## Lint with ruff.
	@$(RUFF) check $(SRC_DIR) $(TESTS_DIR)

format: ## Format with ruff (in-place).
	@$(RUFF) format $(SRC_DIR) $(TESTS_DIR)
	@$(RUFF) check --fix $(SRC_DIR) $(TESTS_DIR)

typecheck: ## Run mypy in strict mode.
	@$(MYPY) $(SRC_DIR)

test: test-unit ## Run all tests (alias for now; integration tier requires Postgres).

test-unit: ## Run fast unit tests (no DB, no network).
	@$(PYTEST) $(TESTS_DIR)/unit -q

test-integration: ## Run integration tests (requires running Postgres+pgvector).
	@$(PYTEST) $(TESTS_DIR)/integration -q

eval: ## Run retrieval eval harness (requires Postgres+pgvector; opt-in).
	@$(PYTEST) $(TESTS_DIR)/eval -q -m "eval"

openapi-lint: ## Validate OpenAPI 3.1 spec.
	@$(PY) scripts/lint_openapi.py openapi/openapi.yaml

security: ## Run security scanners (bandit + pip-audit + sbom + grype).
	@$(PY) -m bandit -q -r $(SRC_DIR) -ll || true
	@$(PY) -m pip_audit --strict || true
	@$(MAKE) sbom
	@$(MAKE) scan

sbom: ## Generate SPDX SBOM via syft.
	@command -v syft >/dev/null 2>&1 || { echo "syft not installed — install from https://github.com/anchore/syft"; exit 1; }
	@syft . -o spdx-json > sbom.spdx.json
	@echo "Wrote sbom.spdx.json"

scan: ## Scan SBOM via grype.
	@command -v grype >/dev/null 2>&1 || { echo "grype not installed — install from https://github.com/anchore/grype"; exit 1; }
	@grype sbom:sbom.spdx.json -o table

run: ## Start the API on $$KILN_HTTP_PORT (default 8080).
	@$(PY) -m uvicorn $(PKG).api.app:app --host 0.0.0.0 --port $${KILN_HTTP_PORT:-8080} --reload

run-worker: ## Start the ingestion worker (uses config/sources.yaml).
	@$(PY) -m $(PKG).ingestion serve-worker --config config/sources.yaml

migrate: ## Apply Alembic migrations (Phase 2+).
	@$(PY) -m alembic upgrade head

migrate-down: ## Roll back one revision.
	@$(PY) -m alembic downgrade -1

ingest: ## Enqueue ingestion jobs for active sources (uses config/sources.yaml).
	@$(PY) -m $(PKG).ingestion ingest --config config/sources.yaml

reembed: ## Re-embed every chunk via the active provider (use after a model swap or prefix fix; #224).
	@$(PY) -m $(PKG).ingestion reembed

reembed-dry-run: ## Preview the reembed chunk count without writing anything.
	@$(PY) -m $(PKG).ingestion reembed --dry-run

cf-push: ## Push to Cloud Foundry using ./manifest.yml.
	@command -v cf >/dev/null 2>&1 || { echo "cf CLI not installed"; exit 1; }
	@cf push -f manifest.yml

cf-verify: ## Static checks against the CF-deploy contract — manifest, requirements, scripts, example configs (#229/#240/#241/#242).
	@$(PY) scripts/cf-verify.py

build-css: ## Concatenate the static/kiln/*.css partials into static/kiln.css.
	@printf '/* GENERATED FILE — DO NOT EDIT.\n   Source: src/cf_knowledge_kiln/api/static/kiln/_*.css\n   Regenerate with `make build-css`; `make verify-css` blocks drift in CI. */\n\n' > src/cf_knowledge_kiln/api/static/kiln.css
	@cat src/cf_knowledge_kiln/api/static/kiln/_tokens.css \
	     src/cf_knowledge_kiln/api/static/kiln/_fonts.css \
	     src/cf_knowledge_kiln/api/static/kiln/_base.css \
	     src/cf_knowledge_kiln/api/static/kiln/_search.css \
	     src/cf_knowledge_kiln/api/static/kiln/_results.css \
	     src/cf_knowledge_kiln/api/static/kiln/_feedback.css \
	     src/cf_knowledge_kiln/api/static/kiln/_preview.css \
	     src/cf_knowledge_kiln/api/static/kiln/_keyboard.css \
	     src/cf_knowledge_kiln/api/static/kiln/_empty.css \
	     src/cf_knowledge_kiln/api/static/kiln/_onboarding.css \
	     src/cf_knowledge_kiln/api/static/kiln/_motion.css \
	     src/cf_knowledge_kiln/api/static/kiln/_print.css \
	     src/cf_knowledge_kiln/api/static/kiln/_forced_colors.css \
	     >> src/cf_knowledge_kiln/api/static/kiln.css

verify-css: ## Rebuild kiln.css from partials and fail if the file drifted.
	@$(MAKE) build-css
	@git diff --exit-code src/cf_knowledge_kiln/api/static/kiln.css \
	  || { echo ""; \
	       echo "✗ kiln.css is out of sync with src/cf_knowledge_kiln/api/static/kiln/*.css partials."; \
	       echo "  Run 'make build-css' and commit the regenerated kiln.css."; \
	       exit 1; }

verify: lint typecheck test openapi-lint verify-css cf-verify ## The local quality gate. Run before pushing.
	@echo ""
	@echo "✓ verify passed"

clean: ## Remove caches and build artifacts.
	@rm -rf build/ dist/ *.egg-info .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
