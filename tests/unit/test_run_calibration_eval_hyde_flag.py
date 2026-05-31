"""#334 smoke test for the calibration-eval CLI's ``--with-hyde`` flag.

The flag is a labeling-only switch — it doesn't toggle HyDE on the
running API. These tests pin:

* The flag is accepted (parsing doesn't crash).
* The output markdown carries the right arm label.
* Default invocation labels as ``baseline``.
* The hyde-label invocation surfaces the ADR-0013 mislabel-warning blurb.

The eval itself needs a running API; tests stub the urlopen calls
so this runs under ``test-unit`` (no DB, no network).
"""

from __future__ import annotations

import io
import urllib.error
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

from tests.eval.run_calibration_eval import main


def _mock_health_ok(url: str, *args: Any, **kwargs: Any) -> Any:
    """Return a fake healthz response object."""

    class _Resp:
        def read(self) -> bytes:
            return b'{"status":"ok"}'

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    return _Resp()


def _mock_search_error(*args: Any, **kwargs: Any) -> Any:
    """Force every search call to raise — the report still renders
    with ERROR rows, which is enough for the labeling assertions."""
    raise urllib.error.URLError("synthetic test failure")


class TestWithHydeFlag:
    def test_default_invocation_labels_baseline(self) -> None:
        """Default invocation (no --with-hyde) labels report as 'baseline'."""
        call_count = {"n": 0}

        def stub(url: str, *args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_health_ok(url)
            raise urllib.error.URLError("synthetic")

        buf = io.StringIO()
        with (
            patch("tests.eval.run_calibration_eval.urllib.request.urlopen", side_effect=stub),
            redirect_stdout(buf),
        ):
            main([])
        out = buf.getvalue()
        assert "baseline" in out.lower(), f"baseline label missing: {out[:200]}"
        # The ADR-0013 warning blurb is only present on the HyDE arm.
        assert "ADR-0013" not in out

    def test_with_hyde_flag_parses(self) -> None:
        """--with-hyde must be an accepted CLI flag (no SystemExit)."""
        # We can't easily run the full eval against a fake API in a
        # unit test, but we can verify the flag parses by giving
        # main() argv and patching urlopen to short-circuit at the
        # health check (returning 2).
        with patch("tests.eval.run_calibration_eval.urllib.request.urlopen") as urlopen:
            urlopen.side_effect = urllib.error.URLError("no API in unit-test env")
            rc = main(["--with-hyde"])
        # Exit 2 = API unreachable. That means the flag parsed and we
        # reached the healthz call.
        assert rc == 2

    def test_with_hyde_label_in_report_header(self) -> None:
        """When the eval DOES reach the report (health-check passes),
        the markdown's first line carries 'WITH HyDE'. We mock the
        health call to succeed then errors on subsequent calls so the
        report renders with all-ERROR rows."""

        call_count = {"n": 0}

        def stub(url: str, *args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # healthz call succeeds
                return _mock_health_ok(url)
            raise urllib.error.URLError("synthetic")

        buf = io.StringIO()
        with (
            patch("tests.eval.run_calibration_eval.urllib.request.urlopen", side_effect=stub),
            redirect_stdout(buf),
        ):
            main(["--with-hyde"])
        out = buf.getvalue()
        assert "WITH HyDE" in out, f"hyde label missing from report header: {out[:200]}"
        # ADR-0013 warning blurb is present.
        assert "ADR-0013" in out
