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

from cf_knowledge_kiln.api.views import (
    agent_guide_url,
    authority_vocabulary,
    feedback_categories,
    score_legend_tiers,
)

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
# #408 F2 + F18: legend helpers iterated by the results-list legend
# (`_results.html`). Vocabulary lives in api.views so a future
# rename touches the legend AND the per-card chip rendering in one
# place; the template just reads the tuple.
templates.env.globals["score_legend_tiers"] = score_legend_tiers
templates.env.globals["authority_vocabulary"] = authority_vocabulary

__all__ = ["TEMPLATES_DIR", "templates"]
