"""Regression guard for the api/__init__.py import cycle (#207).

PR #196 added ``from cf_knowledge_kiln.api.tracing import get_tracer``
inside ``retrieval/engine.py``. Before #208, ``api/__init__.py``
eagerly re-exported ``app`` and ``create_app``, so any import of an
``api.X`` submodule from outside ``api/`` triggered the full app
import chain (``app`` → ``preview`` → ``dependencies`` →
``retrieval``) — and that closed the cycle.

The fix was to drop the eager re-exports. This test asserts that no
one accidentally re-introduces them by importing ``retrieval.engine``
in a fresh subprocess (so it has no pre-loaded ``cf_knowledge_kiln``
state, which is what masks the cycle in pytest's normal run).
"""

from __future__ import annotations

import subprocess
import sys


def test_fresh_subprocess_import_of_retrieval_engine_does_not_cycle() -> None:
    """A clean ``python -c "import cf_knowledge_kiln.retrieval.engine"`` must succeed.

    Run in a subprocess so the cycle-masking that pytest's pre-loads
    provide can't hide a regression. The import path goes through
    ``api.tracing.get_tracer`` at module load — if ``api/__init__.py``
    starts eagerly loading ``app`` again, that import will raise an
    ``ImportError`` for ``HybridRetriever`` from the partially
    initialized ``retrieval`` package.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import cf_knowledge_kiln.retrieval.engine"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"fresh import of cf_knowledge_kiln.retrieval.engine failed — "
        f"likely a re-introduced cycle via api/__init__.py.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_fresh_subprocess_import_of_api_tracing_does_not_pull_in_app() -> None:
    """Importing ``api.tracing`` must NOT eagerly load ``api.app``.

    The point of #208 was to make ``api/__init__.py`` import-light.
    If a future change adds ``from ... import app`` back to
    ``api/__init__.py``, this test fails loudly.
    """
    script = (
        "import sys\n"
        "from cf_knowledge_kiln.api import tracing  # noqa: F401\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('cf_knowledge_kiln.api'))\n"
        "print('\\n'.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"import failed: {result.stderr}"
    loaded = result.stdout.strip().splitlines()
    # The set we expect from importing just `cf_knowledge_kiln.api.tracing`:
    # the package itself and the leaf module. NOT app, dependencies, etc.
    forbidden = {
        "cf_knowledge_kiln.api.app",
        "cf_knowledge_kiln.api.dependencies",
        "cf_knowledge_kiln.api.preview",
        "cf_knowledge_kiln.api.retrieval",
        "cf_knowledge_kiln.api.web",
    }
    leaked = forbidden & set(loaded)
    assert not leaked, (
        f"importing api.tracing leaked these api submodules into sys.modules: "
        f"{sorted(leaked)}. The api/__init__.py is eager-loading again."
    )
