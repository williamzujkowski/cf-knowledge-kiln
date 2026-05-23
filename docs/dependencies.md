# Dependency management

The repo uses **`uv.lock`** (Astral's lockfile) as the source of truth for
which exact versions of which transitive dependencies a deploy ships
with. `pyproject.toml` keeps the human-edited *ranges*; `uv.lock`
records the resolved versions plus their hashes.

CI installs from `uv.lock` (`uv sync --frozen`), so the same commit
deploys to the same set of pinned versions every time. Without this,
two `pip install -e .[…]` runs against the same commit could resolve
to different transitive trees — and one of them might pick up a
transitive CVE the other doesn't have.

## Daily workflow

You don't normally interact with the lockfile. Just edit
`pyproject.toml` like before, then refresh the lock when you're done:

```bash
make lock          # regenerates uv.lock against pyproject.toml
```

Commit `uv.lock` alongside your `pyproject.toml` change. CI uses
`uv sync --frozen` — if your PR updated `pyproject.toml` without
re-locking, CI will fail at the install step with
``error: lockfile is out of date`` and tell you to run `uv lock`.

If you don't have `uv` installed:

```bash
pip install uv         # one-time
# or: pipx install uv  # if you prefer isolated CLIs
```

The repo's `Makefile` `bootstrap` target already prefers `uv` when
available (`uv pip install --system -e ".[dev]"`).

## What `uv sync --frozen` does

* Reads `uv.lock`, refuses to mutate it.
* Creates `.venv/` next to `pyproject.toml` (gitignored).
* Installs the project (editable) + the requested extras at exactly
  the versions and hashes recorded in the lock.
* If your `pyproject.toml` is incompatible with the lock (you added /
  removed / re-ranged a dep), it fails fast.

CI installs `.[dev,db,ingestion]`. The optional `[real-embeddings]`
and `[otel]` extras are not in the default sync set — both are
deploy-time choices (heavy local-inference deps; opt-in tracing) that
operators install separately.

## Refreshing the lockfile

```bash
make lock                    # full refresh against current pyproject
uv lock --upgrade-package fastapi   # bump just one package's pin
uv lock --upgrade            # try to bump everything within ranges
```

After any of these, run `make verify` and `make test-integration`
locally before pushing. The lockfile change should be its own commit
(or paired with the `pyproject.toml` edit that motivated it) so the
diff is reviewable.

## Dependabot

`.github/dependabot.yml` already covers `pip` (the `pyproject.toml`
ranges) and `github-actions`. Dependabot PRs that bump a range will
trip the `--frozen` check; the bot needs to also regenerate
`uv.lock` for those PRs to pass CI. If a dependabot PR fails on
``lockfile is out of date``, run `make lock` locally and push the
refreshed lock to the PR's branch.

## Why not `pip-tools` or `poetry`?

`uv` is fast (Rust; resolutions complete in ~1s for this project) and
its lockfile format records dependency markers + extras + hashes in a
single self-contained TOML. The Makefile and pre-commit hooks
already prefer `uv` over `pip` when available, so adopting it as the
lockfile tool is a small step forward, not a stack switch.
