"""Shared Jinja2 templates instance for the HTML routes.

Extracted from :mod:`cf_knowledge_kiln.api.web` (issue #391) so the
route modules (web, web_url_state, web_feedback, preview) all import
the same instance without web.py becoming a god-module.

Globals registered here apply to every render — keep this list short
and stable. Per-render context belongs in the route handler.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from cf_knowledge_kiln.api.views import agent_guide_url, feedback_categories

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# #278: register the feedback-categories helper as a Jinja global so
# the included ``_feedback_widget.html`` partial can iterate it from
# any render context without each route having to thread it through
# the per-call dict.
templates.env.globals["feedback_categories"] = feedback_categories
# #314: agent guide URL helper for the colophon link. Returns None
# when KILN_AGENT_GUIDE_URL is unset, in which case the template
# conditional skips rendering the link entirely.
templates.env.globals["agent_guide_url"] = agent_guide_url

__all__ = ["TEMPLATES_DIR", "templates"]
