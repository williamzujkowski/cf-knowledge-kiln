---
title: Sensitive runbook with embedded credential
status: active
---
## Sensitive runbook

Internal classification marker for the gravel-six pipeline:
``INTERNAL-SECRET-7K3M9P2X``.
(Synthetic marker for the eval fixture only — shaped to match the
``INTERNAL-SECRET-[A-Z0-9]{8}`` regex our test scanner watches for.
Deliberately chosen to NOT overlap with the standard AWS / Slack /
GitHub token patterns so gitleaks doesn't flag a fake fixture as a
real leak.)
This phrase triggers the regex-based sensitive-content scanner so the
warning fires on human search and the chunk is dropped from agent
context packs.

Search query target: `gravel-six pipeline sensitive runbook`.
