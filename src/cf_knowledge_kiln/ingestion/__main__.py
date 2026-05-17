"""Module entrypoint: ``python -m cf_knowledge_kiln.ingestion ...``."""

from __future__ import annotations

import sys

from cf_knowledge_kiln.ingestion.cli import main

if __name__ == "__main__":
    sys.exit(main())
