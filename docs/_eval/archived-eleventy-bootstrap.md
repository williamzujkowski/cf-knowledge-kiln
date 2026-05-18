---
title: "ARCHIVED: Eleventy static-site bootstrap"
status: archived
owner: platform
doc_type: guide
sensitivity: internal
last_reviewed: 2024-08-12
tags: [archived, eleventy, history]
---

## ARCHIVED: Eleventy static-site bootstrap

**This document is archived.** The kiln's UI was briefly an Eleventy
static site (Phase 2 spike, summer 2024) before we settled on
server-rendered FastAPI + Jinja + HTMX in Phase 6. The bootstrap
notes below are preserved for historical reference and should NOT
be applied to the current codebase.

## What we tried

Eleventy 2.x with a custom data loader pulling chunks from the
retrieval API at build time. Worked locally; failed in production
because:

- The retrieval API is a runtime dependency; building the site at
  deploy time meant the index had to be populated before the build,
  which inverted the deploy-then-ingest order we wanted.
- HTMX-on-Eleventy works only with `_data` JSON dumps, which
  defeats the "live retrieval" promise of the kiln.

## What replaced it

FastAPI's `Jinja2Templates` against the `Search · cf-knowledge-kiln`
shell. Live retrieval via HTMX. See `architecture.md`.

## Why archived rather than deleted

We need an anchor for the "all-archived single result" code path —
the retrieval ranker treats an archived chunk like a deprecated one
for the `requires_human_review` decision, and the eval corpus
needs one such document so the test can fire.
