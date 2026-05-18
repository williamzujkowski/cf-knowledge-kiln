---
title: "Guide: when to use /v1/search vs /v1/agent/context-pack"
status: active
owner: platform
doc_type: guide
sensitivity: internal
last_reviewed: 2026-05-05
tags: [guide, api, agent, search]
---

## Guide: `/v1/search` vs `/v1/agent/context-pack`

Two endpoints, one retrieval engine, two response shapes. Pick by
consumer type, not by query complexity.

## Use `/v1/search` when

- A human will read the response.
- Result cards need previews, freshness badges, feedback links.
- You want all 20 (or more) top-K results to inspect.

`/v1/search` returns `ResultCard[]` with rich fields:
`title`, `excerpt`, `heading_path`, `source_url`, `last_reviewed`,
`score`. No token budget. No untrusted-content notice (the human
ribbon at the top of the page carries that).

## Use `/v1/agent/context-pack` when

- An LLM will consume the response inside a prompt.
- You want a bounded context — `token_budget` + warnings +
  `requires_human_review` are non-negotiable.
- The pack must be cited (every chunk carries the four-part citation
  from `standard-citation-format.md`).

`/v1/agent/context-pack` returns a `ContextPack` with `chunks`,
`warnings`, `token_budget`, `requires_human_review`, and the
`untrusted_content_notice` preamble required by AGENTS.md.

## Picking the wrong one

The two shapes are not interchangeable. An agent that consumes
`ResultCard[]` will overshoot its token budget and miss the
warning preamble; a human UI that consumes `ContextPack` loses the
freshness signals and the feedback widget surface area.

If the calling code crosses from one to the other (e.g., a tool that
proxies the search API for an agent), the proxy MUST construct a
context pack from the result cards, not forward them verbatim.
