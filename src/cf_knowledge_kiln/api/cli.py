"""Tiny CLI entrypoint.

Exposed as ``cf-knowledge-kiln`` via the ``[project.scripts]`` table.
Phase 1 only supports starting the API; later phases add ``ingest``,
``migrate``, and ``query``.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from cf_knowledge_kiln import __version__
from cf_knowledge_kiln.config import get_settings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="cf-knowledge-kiln")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host")  # noqa: S104
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (overrides settings)")
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload (dev only)")

    args = parser.parse_args(argv)

    if args.command == "serve":
        settings = get_settings()
        port = args.port if args.port is not None else settings.http_port
        uvicorn.run(
            "cf_knowledge_kiln.api.app:app",
            host=args.host,
            port=port,
            reload=args.reload,
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
