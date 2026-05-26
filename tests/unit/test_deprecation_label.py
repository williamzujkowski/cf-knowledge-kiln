"""Unit tests for ``api.views.deprecation_label`` (#268).

The Python helper drives the editorial stamp text on result cards.
Three non-current statuses map to status-specific verbal copy; the
three current statuses map to None so the template's
``{% if r.deprecation_label %}`` cleanly suppresses the stamp.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.api.views import deprecation_label


class TestDeprecationLabel:
    """Editorial stamp policy — pin every status's mapping.

    A future status addition that doesn't map to a deprecation label
    will silently get no stamp (which is correct for active/approved/
    draft). A future deprecation-class status MUST be added to the
    table AND covered here so we don't ship a stripe-only signal that
    the audit already flagged as too subtle.
    """

    def test_deprecated_status(self) -> None:
        assert deprecation_label("deprecated") == "Deprecated · do not cite"

    def test_archived_status(self) -> None:
        assert deprecation_label("archived") == "Archived · historical reference"

    def test_superseded_status(self) -> None:
        assert deprecation_label("superseded") == "Superseded · see successor"

    @pytest.mark.parametrize("status", ["active", "approved", "draft"])
    def test_current_statuses_have_no_stamp(self, status: str) -> None:
        """Absence of a stamp is the signal that the card is current.
        Returning None lets the template skip the entire span without
        a per-status conditional."""
        assert deprecation_label(status) is None

    def test_unknown_status_has_no_stamp(self) -> None:
        """Forward-compat: a future status not in the table gets no
        stamp. Better than silently flagging an unknown status as
        deprecated (which would over-warn) or as current (which the
        spec calls 'a bug, not a feature' — but only for the known
        deprecated set)."""
        assert deprecation_label("future-status") is None

    def test_verbal_copy_uses_middle_dot_separator(self) -> None:
        """The editorial voice uses U+00B7 MIDDLE DOT as the
        separator (e.g. 'Deprecated · do not cite'), NOT an ASCII
        '-' or em-dash. The dot pairs with Fraunces's small-caps
        kerning to read as a typographic mark rather than a hyphen.
        This test pins the choice so a future copy refactor doesn't
        silently swap to ASCII."""
        assert "·" in deprecation_label("deprecated")
        assert "·" in deprecation_label("archived")
        assert "·" in deprecation_label("superseded")
