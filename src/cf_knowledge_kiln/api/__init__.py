"""HTTP API (FastAPI).

Submodules are imported explicitly by their fully-qualified path —
``cf_knowledge_kiln.api.app:app`` for the uvicorn entry point,
``from cf_knowledge_kiln.api.app import create_app`` for tests.

The package ``__init__`` deliberately re-exports nothing (#207). Eagerly
loading ``app`` here caused a circular import: any module outside
``api/`` that imported a sibling helper (``api.tracing`` after #196,
``api.observability``) triggered ``api/__init__.py``, which loaded
``api.app`` → ``api.preview`` → ``api.dependencies`` → back into
``retrieval``. ``retrieval.engine`` legitimately imports
``api.tracing.get_tracer`` (the no-op-safe tracer accessor), so the
cycle was reachable from a fresh interpreter ``import
cf_knowledge_kiln.retrieval.engine``.
"""
