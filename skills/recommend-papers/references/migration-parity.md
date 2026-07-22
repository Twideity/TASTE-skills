# Migration parity contract

This file distinguishes migrated deterministic semantics from deliberate user-directed replacements. “Similar behavior” is not parity.

## Finding semantics that are locked

- A resolved venue/year is acquired as a complete corpus before topical filtering. Venue sources forbid query/category/track/result limits.
- Priority NeurIPS, ICLR, and ICML metadata requires complete abstracts; ICLR and ICML also require official categories. A complete title-only DBLP volume is not sufficient for those priority metadata policies.
- OpenReview uses authenticated official venue notes and paginates to exhaustion. DBLP complete-volume XML is a title-index/cross-check adapter, not a capped search result.
- Other non-venue sources retain the original 5000-record default. The authoritative original arXiv and bioRxiv paths also used that cap and task-filtered retrieval; both are superseded below at the user's direction.
- The complete source payload is cached before Codex semantic ranking. The authoritative original Find configuration used a global title/abstract scoring limit of 1000.

## Reading semantics that are locked

- A full text is accepted only for the same paper, has at least 1200 characters, and contains paper-body evidence.
- Cache identity uses DOI/arXiv/OpenReview/PMCID/URL/title-author-year aliases. Conflicting exact identifiers never reuse an artifact.
- Full-text cache binds extracted text and PDF hashes into a content revision. A changed revision invalidates a cached single-paper read.
- Every ready full text requires one Chinese `papers/<index>/read.md`; fixed title/metadata/five sections, Chinese translation checks, formula delimiter checks, and the original length requirements are enforced.
- Preserve the original dedicated per-paper reading-agent semantics: each paper has a machine receipt attesting whole-full-text inspection. Without a supplied Chinese abstract, `摘要` is the complete Chinese translation of the original English abstract, not a generated summary.
- Valid single-paper reads are aggregated to run-level `read.md`; final evidence must cite both exact full text and exact validated read paths.
- Final unified Reading ranking uses the original two 0–10 dimensions, `match_score` and `transferability_score`, after reading every completed single-paper artifact.

## Deliberate replacements required by the user

- Codex replaces the former Find/Reading LLM or Claude for query planning, category judgment, paper scoring, deep reading, and synthesis.
- At the user's explicit direction on 2026-07-22, the default paper-level full-text/deep-reading shortlist is 100 instead of the original Find value of 1000; the default final recommendation count remains 20. Both are user-overridable.
- Initial ranking now uses title and abstract alone and sends only its top results to full-text acquisition. Maximum available Codex subagent concurrency is used in waves, with exactly one shortlisted paper per dedicated subagent.
- At the user's explicit direction on 2026-07-22, arXiv no longer uses the original 5000-result/query cache. It exhaustively caches category/date metadata as `arxiv/<category>/<day>.json` and postpones task-keyword filtering to Codex metadata scoring. The initial eight-category AI set was subsequently expanded by the user to include `cs.IR` and `eess.SY`, ensuring default retrieval/RAG and embodied-control coverage alongside `cs.CL`, `cs.CV`, and `cs.RO`.
- At the user's explicit direction on 2026-07-22, bioRxiv likewise replaces its original task-filtered 5000-result range cache with exhaustive all-category daily shards as `biorxiv/<day>.json`. The current day remains provisional, the preceding three closed days use a 24-hour reconciliation window, older proven-complete days persist until explicit refresh, and DOI versions are retained in raw shards before latest-version aggregation.
- Runtime state and caches live outside Git repositories, and the service must remain independent of `modules/finding` and `modules/reading`.
- User-requested default discovery scope is latest usable NeurIPS/ICLR/ICML, trailing-six-month arXiv, and one to three topic-adaptive channels.
- HTTP 429/503 retries and cross-process throttling are reliability enhancements requested after observed failures; the original Find arXiv path recorded rate limiting and stopped with partial results rather than performing this retry loop.
- Non-semantic integrity enhancements bind receipts to full-text/source-abstract/read hashes, require a complete source-sentence translation map, reject only exactly duplicated long sections across distinct papers, and version reading caches. These strengthen validation without replacing the original Reading content rules; no fuzzy similarity threshold is used.

## Certification rule

Do not claim migration parity merely because tests pass. Certification requires structural tests, representative live-source receipts, cache-reuse/revision tests, and a manual comparison against the authoritative original functions named in the repository history. Any known deviation must be reported explicitly.
