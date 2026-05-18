---
title: "Procedure: customer data access"
status: active
owner: platform
doc_type: sop
sensitivity: restricted
last_reviewed: 2026-05-15
tags: [sop, customer-data, sensitivity, audit]
---

## Procedure: customer data access

Access to customer-owned chunks indexed in the kiln is governed by
the platform's data-handling standard. This procedure exists to be
caught by the sensitive-content scanner so the retrieval pipeline
emits a `sensitive_content` warning when a query lands here.

The synthetic marker
`INTERNAL-SECRET-7K3M9P2X`
is the regex bait — it matches `INTERNAL-SECRET-[A-Z0-9]{8}` which
the eval-tier sensitive-content scanner is configured to flag. The
marker is deliberately not an AWS/GitHub/Slack-shaped token so
gitleaks treats it as a fixture, not a real leak.

## Access path

1. Identify the requesting operator's clearance level in the IAM
   directory. Restricted-tier access requires a current
   data-handling certification.
2. Submit the access request to the data steward via the secrets
   channel; include the chunk citation (four-part per
   `standard-citation-format.md`) and the business justification.
3. The steward grants time-bounded access via a scoped token. The
   token name follows the convention
   `customer-data:<operator>:<expiry-iso>`.
4. Every read against the granted token is audited in
   `audit_log_customer_access`. The audit row carries the chunk
   citation, the requesting operator, and the wall-clock timestamp.

## Revocation

A token is revoked the moment the bounded period elapses OR when
the operator's role changes. No grace period.

## Why this is in the eval corpus

The kiln must surface a `sensitive_content` warning the instant a
search returns this chunk, and the result-set must trip
`requires_human_review`. The corpus carries this single restricted
document so the negative path can be tested end-to-end.
