"""Calibration eval harness for the homelab-iac golden set (#222, #228).

Runs the 15-query golden set from homelab-iac#627 (13 positives,
2 negatives) against ``POST /v1/search`` on the local API and prints
a markdown report with:

- Top-1 score distribution (min, p25, median, mean, p75, max) — the
  ``floor-index`` percentile convention is used
  (``sorted[(p*n)//100]``), matching the e5 + Nomic calibration
  reports so they're comparable.
- Per-query top-1 score, expected source, and
  ``in-top-5`` / ``missed`` / ``leaked-through`` classification.

Use cases:

* Before/after a ``sentence-transformers`` / ``torch`` floor bump
  (#222 — calibration is preserved if the top-1 distribution doesn't
  shift below the configured ``weak_evidence_score_threshold``).
* Embedder-swap experiments (#228 — measure top-5 hit rate, not just
  threshold pass-through).

Requires:

* A running API at ``http://127.0.0.1:8000`` (uvicorn) with the same
  ``config/models.yaml`` the corpus was ingested at.
* The homelab-iac corpus ingested into the local Postgres (see
  ``config/sources.local.yaml``).

Designed for ``/v1/search`` since it's the simplest path;
``/v1/answer`` adds generator latency to the loop without changing
the underlying retrieval score these reports track.
"""

from __future__ import annotations

import json
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "http://127.0.0.1:8000"


@dataclass
class Query:
    qid: str
    text: str
    expected: list[str]  # substrings of repo/path in evidence; empty for negatives
    is_negative: bool = False


# From homelab-iac issue #627 (kiln-test A.4 golden set).
QUERIES: list[Query] = [
    Query("q01", "How do I rotate the restic passphrase for the offsite backup?",
          ["skills/manage-offsite-backup"]),
    Query("q02", "What's the BBR director restore command?",
          ["skills/bbr-backup-and-restore", "docs/components/backups-bbr"]),
    Query("q03", "What does the CredHubCAExpiring alert mean and what do I do?",
          ["docs/runbooks/credhubcaexpiring"]),
    Query("q04", "OffsiteBackupSizeAnomaly fired — likely cause?",
          ["docs/runbooks/offsitebackupfailed"]),
    Query("q05", "How does TLS work in the lab? Where do the certs come from?",
          ["docs/components/caddy-reverse-proxy", "AGENTS.md"]),
    Query("q06", "What's the offsite backup architecture?",
          ["docs/components/offsite-backup"]),
    Query("q07", "Why do we have two offsite backup tiers and what does each protect against?",
          ["docs/components/offsite-backup", "docs/components/backups-bbr"]),
    Query("q08", "Where is the Cloudflare DNS token stored and what uses it?",
          ["AGENTS.md"]),
    Query("q09", "If I reboot pi-b, how do I tell when SSH is reachable again from the workstation?",
          ["AGENTS.md", "scripts/wait-for-host"]),
    Query("q10", "What's the trap with re-keying restic vs. regenerating the passphrase?",
          ["skills/manage-offsite-backup"]),
    Query("q11", "Which IPs are on VLAN 40 and what runs there?",
          ["AGENTS.md", "inventory/lab.yml"]),
    Query("q12", "What does pi-a do?",
          ["docs/components/authentik-sso", "inventory/lab.yml"]),
    Query("q13", "What's the procedure for failing over to AWS?", [], is_negative=True),
    Query("q14", "How do I configure Kubernetes?", [], is_negative=True),
    Query("q15", "What's the current pgvector deployment status?",
          ["docs/components/pgvector"]),
]


def search(q: str, k: int = 5) -> dict:
    req = urllib.request.Request(
        f"{API}/v1/search",
        data=json.dumps({"query": q, "max_results": k}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    try:
        urllib.request.urlopen(f"{API}/healthz", timeout=5).read()
    except urllib.error.URLError as exc:
        print(f"API unreachable at {API}: {exc}", file=sys.stderr)
        return 2

    rows = []
    positives_top1 = []
    positives_expected_in_top5 = 0
    positive_count = 0
    negatives_correctly_flagged = 0
    negative_count = 0

    for q in QUERIES:
        try:
            r = search(q.text)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            rows.append((q.qid, q.text, "ERROR", str(exc), False))
            continue
        results = r.get("results") or []
        if not results:
            top1 = 0.0
            expected_hit = False
        else:
            top1 = float(results[0].get("score") or 0.0)
            # Expected-source check: does any of top-5's repo/path contain
            # one of the expected substrings?
            top5_locations = []
            for res in results[:5]:
                loc = f"{res.get('repo') or ''}/{res.get('path') or ''}"
                top5_locations.append(loc)
            expected_hit = any(
                any(exp in loc for loc in top5_locations) for exp in q.expected
            )
        if q.is_negative:
            negative_count += 1
            # "Correctly flagged" = warnings include weak_evidence OR
            # top1 below 0.46 OR no results.
            warnings = r.get("warnings") or []
            warning_types = {w.get("type") for w in warnings}
            flagged = "weak_evidence" in warning_types or top1 < 0.46 or not results
            if flagged:
                negatives_correctly_flagged += 1
            rows.append(
                (q.qid, q.text, top1, "negative",
                 "flagged" if flagged else "leaked-through")
            )
        else:
            positive_count += 1
            positives_top1.append(top1)
            if expected_hit:
                positives_expected_in_top5 += 1
            rows.append(
                (q.qid, q.text, top1, ",".join(q.expected),
                 "in-top-5" if expected_hit else "missed")
            )

    # ── report ──
    print("# Calibration eval — homelab-iac corpus")
    print()
    print(f"Active embedding: from `config/models.yaml`. Queries: {len(QUERIES)} "
          f"({positive_count} positives, {negative_count} negatives).")
    print()

    if positives_top1:
        sorted_top1 = sorted(positives_top1)
        n = len(sorted_top1)
        median = statistics.median(sorted_top1)
        mean = statistics.mean(sorted_top1)
        p25 = sorted_top1[max(0, n // 4)]
        p75 = sorted_top1[min(n - 1, (3 * n) // 4)]
        print("## Top-1 score distribution (positive queries)")
        print()
        print("| stat | value |")
        print("| ---- | ----- |")
        print(f"| min | {min(sorted_top1):.3f} |")
        print(f"| p25 | {p25:.3f} |")
        print(f"| median | {median:.3f} |")
        print(f"| mean | {mean:.3f} |")
        print(f"| p75 | {p75:.3f} |")
        print(f"| max | {max(sorted_top1):.3f} |")
        print()
    print(f"Positive expected-in-top-5: {positives_expected_in_top5}/{positive_count}")
    print(f"Negatives correctly flagged: {negatives_correctly_flagged}/{negative_count}")
    print()
    print("## Per-query")
    print()
    print("| qid | top1 | category | result |")
    print("| --- | ---- | -------- | ------ |")
    for qid, text, top1, category, result in rows:
        top1_str = f"{top1:.3f}" if isinstance(top1, float) else str(top1)
        # Truncate text for the table.
        t = text if len(text) <= 60 else text[:57] + "..."
        print(f"| {qid} | {top1_str} | {category[:40]} | {result} |  <!-- {t} -->")
    return 0


if __name__ == "__main__":
    sys.exit(main())
