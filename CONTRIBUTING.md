# Contributing

Thanks for considering a contribution. This project is in active early-phase development; the implementation plan lives at [plans/cf-rag-plan.md](./plans/cf-rag-plan.md).

## Before you start

1. Read [AGENTS.md](./AGENTS.md). The disciplines listed there (TDD, YAGNI, DRY, cited-or-silent retrieval, untrusted-input handling) are enforced.
2. Check open issues. Look for the `good-first-issue` label.
3. For non-trivial changes, open or comment on an issue first to align on approach.

## Development setup

```bash
make bootstrap   # install dev deps (uv + pip)
make install     # install the package in editable mode
make verify      # confirm your environment passes the local gate
```

Requires Python 3.12+ and PostgreSQL 15+ with the `pgvector` extension (only needed for integration tests).

## Branching and commits

- Branch from `main`. Branch names: `feat/<short-slug>`, `fix/<short-slug>`, `docs/<short-slug>`, `chore/<short-slug>`.
- Commit messages: imperative present tense, ≤72 char subject. Body explains *why*, not *what* (the diff shows what).
- Reference the issue number: `feat: hybrid retrieval ranking (#42)`.
- Sign your commits if your fork is configured for it.

## Pull requests

- Keep PRs small. Under 400 net lines is a good target.
- Every PR must pass `make verify`.
- New behavior needs tests. Bug fixes need a regression test that fails without the fix.
- API changes update `openapi/openapi.yaml` in the same PR.
- Schema changes ship with a migration in the same PR.

## Code style

- `ruff format` is the formatter. `ruff check` is the linter.
- `mypy --strict` is the type-checker (strict mode is enabled in `pyproject.toml`).
- Files stay under ~400 lines. Functions stay under ~50 lines.
- Public functions have type hints and a one-line docstring. Skip docstrings for obvious private helpers.

## Tests

- `tests/unit/` — fast, no DB, no network.
- `tests/integration/` — real Postgres + pgvector. Use the test fixtures under `tests/fixtures/`.
- Run `make test-unit` while iterating; `make test` (which runs both) before pushing.

## Security findings

If you find a security issue, **do not** file a public GitHub issue. See [SECURITY.md](./SECURITY.md).

## License

By contributing you agree your contributions will be licensed under the MIT License (see [LICENSE](./LICENSE)).
