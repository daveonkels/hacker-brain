# Development measurements

These measurements describe the October 13–16, 2026 sample and the October 16 implementation. They are observations from development, not an independent benchmark. Raw datasets and per-query reports remain local and are not included in this repository.

## Corpus

The collection window was `[2026-10-13T15:51:50Z, 2026-10-16T15:51:50Z)`. The scan accounted for 48,337 IDs and exported 46,269 items created within the window, including 41,021 comments and 5,246 stories. There were no failed or null IDs in that scan. Dead/deleted flags overlap these counts; not every comment had readable text.

The compact export was 18,208,426 bytes and the gzip export 6,469,213 bytes. Raw scan chunks intentionally duplicate the export for resumability. These export figures therefore do not represent the total working-directory size.

Ancestor backfill added 4,639 items, about 2.52 MB, with no unresolved parent IDs in this sample. The resulting search index held 50,908 items and 41,489 searchable comments. The semantic index contained 41,935 passages, occupied 172.83 MB, and took about 19 minutes to build on the development Mac.

## Quick Decisions latency

The final transport uses one proxy session per search and up to 16 independent API requests in flight. Each shortlisted comment can require a reusable classification and a separate question-specific relevance request. Quick assesses at most 48 comments; Broader assesses up to 160.

| New question | Total time | Retrieval | Reused static labels | Cached question ratings |
| --- | ---: | ---: | ---: | ---: |
| Which open models do people use most in their everyday workflow? | 8.284 s | 0.286 s | 47/48 | 0/48 |
| How are local language models used in daily workflows? | 8.634 s | 0.331 s | 48/48 | 0/48 |
| What problems have people run into using SQLite in production? | 7.723 s | 0.315 s | 20/48 | 0/48 |

These are fresh **questions**, not cold-start runs with empty classification caches. Repeat questions took about 0.05–0.06 seconds inside the engine and made no additional API requests. Browser display adds overhead.

The slowest individual API round trips in these runs took 4.5–6.4 seconds, and final ranking waits for all judgments. A 2–5 second fresh-query target has not been reliably achieved. Earlier full-pool ranking took roughly 46–150 seconds; the smaller shortlist reduces latency at the cost of coverage. Local Gemma classification required roughly 24 minutes of model time across resumed passes for the original expanded-pool development check.

## Retrieval check

The original local-workflow question had five preselected examples: HN comments `50079078`, `50099161`, `50110082`, `50100012`, and `50100773`.

- Original semantic retrieval: 2/5 in the first 20 results.
- Expanded local Gemma reranking: 4/5 in the first 20, all five within the first 40.
- Quick Decisions: 4/5 in the first 20, at ranks 4, 1, 5, and 3; the fifth was outside the shortlist.

The questions and examples informed prompts, retrieval, and ranking changes. These results do not establish corpus-wide recall, precision, or factual accuracy. Manual review still found ambiguity around locally run tools that call hosted models, and tool-building reports versus ongoing workflows.

Further work should use held-out questions, independently reviewed relevance judgments, repeated latency measurements, and explicit cold/warm cache reporting.
