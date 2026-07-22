# Migration parity contract

This file distinguishes migrated deterministic semantics from deliberate user-directed replacements. “Similar behavior” is not parity.

## Finding semantics that are locked

- A resolved venue/year is acquired as a complete corpus before topical filtering. Venue sources forbid query/category/track/result limits.
- All 14 priority conference channels require complete abstracts: NeurIPS/NIPS, ICLR, ICML, SIGKDD/KDD, SIGIR, CIKM, AAAI, ICCV, WWW, CVPR, ACL, IJCAI, ECCV, and EMNLP. ICLR and ICML additionally require official categories. A complete title-only DBLP volume is never a verified priority-venue cache.
- Preserve the original official-source policy: OpenReview for ICLR; official/OpenReview metadata for NeurIPS and ICML; CVF for CVPR/ICCV; ACL Anthology for ACL/EMNLP; AAAI OJS; IJCAI proceedings; ECVA for ECCV; and official/ACM proceedings with the original indexed-abstract enrichment policy for KDD/SIGIR/CIKM/WWW.
- OpenReview uses authenticated official venue notes and paginates to exhaustion. DBLP year indexes are resolved to every actual TOC volume before XML acquisition (including split KDD and WWW companion volumes); these records remain title-index/cross-check seeds, not capped search results or substitutes for abstracts.
- Current-year availability is determined only by probing actual configured channels. A future proceedings date or missing archive directory must not suppress probes of accepted-paper pages, OpenReview groups, virtual sites, OJS issues, ACL XML, or live indexes. IJCAI's official accepted-papers page is a valid pre-proceedings metadata source when it exposes the exhaustive title/author/abstract collection.
- Other non-venue sources retain the original 5000-record default. The authoritative original arXiv and bioRxiv paths also used that cap and task-filtered retrieval; both are superseded below at the user's direction.
- The complete source payload is cached before Codex semantic ranking. The authoritative original Find configuration used a global title/abstract scoring limit of 1000.

## Reading semantics that are locked

- A full text is accepted only for the same paper, has at least 1200 characters, and contains paper-body evidence.
- Cache identity uses DOI/arXiv/OpenReview/PMCID/URL/title-author-year aliases. Conflicting exact identifiers never reuse an artifact.
- Conference acquisition derives deterministic official PDF candidates for NeurIPS, ICLR, ICML/PMLR, ACM venues, AAAI OJS, CVF, ACL Anthology, IJCAI, and ECVA and tries them before any optional OA-index lookup.
- Full-text cache binds extracted text and PDF hashes into a content revision. A changed revision invalidates a cached single-paper read.
- In default Claude mode, every ready full text requires one Chinese `papers/<index>/read.md`; fixed title/metadata/five sections, Chinese translation checks, formula delimiter checks, and the original length requirements are enforced.
- Default Claude mode preserves the original dedicated per-paper reading-agent semantics: each paper has a machine receipt attesting whole-full-text inspection. Without a supplied Chinese abstract, `摘要` is the complete Chinese translation of the original English abstract, not a generated summary.
- Default-mode valid single-paper reads are aggregated to run-level `read.md`; its final evidence cites both exact full text and exact validated read paths. The explicit/no-Claude fast fallback is the documented deliberate exception.
- Final unified Reading ranking uses the original two 0–10 dimensions, `match_score` and `transferability_score`, after reading every completed single-paper artifact.

## Deliberate replacements required by the user

- Codex replaces the former Finding LLM for query planning, category judgment, metadata scoring, final comparison, and synthesis. Default deep reading again uses TASTE-style external Claude processes.
- At the user's explicit direction on 2026-07-22, the default paper-level full-text/deep-reading shortlist is 100 instead of the original Find value of 1000; the default final recommendation count remains 20. Both are user-overridable.
- Initial ranking uses title and abstract alone and sends only its top results to full-text acquisition. Default Reading gives each paper a fresh external Claude process through a continuously refilled pool capped at 16 concurrent processes. The explicit/no-Claude fallback is a deliberate speed mode: exactly three Codex subagents each directly read one balanced multi-paper batch.
- At the user's explicit direction on 2026-07-22, arXiv no longer uses the original 5000-result/query cache. It exhaustively caches category/date metadata as `arxiv/<category>/<day>.json` and postpones task-keyword filtering to Codex metadata scoring. The initial eight-category AI set was subsequently expanded by the user to include `cs.IR` and `eess.SY`, ensuring default retrieval/RAG and embodied-control coverage alongside `cs.CL`, `cs.CV`, and `cs.RO`.
- At the user's explicit direction on 2026-07-22, bioRxiv likewise replaces its original task-filtered 5000-result range cache with exhaustive all-category daily shards as `biorxiv/<day>.json`. The current day remains provisional, the preceding three closed days use a 24-hour reconciliation window, older proven-complete days persist until explicit refresh, and DOI versions are retained in raw shards before latest-version aggregation.
- Runtime state and caches live outside Git repositories, and the service must remain independent of `modules/finding` and `modules/reading`.
- User-requested default discovery scope is latest usable NeurIPS/ICLR/ICML, trailing-six-month arXiv, and one to three topic-adaptive channels.
- At the user's direction, that comprehensive scope and the 100/20 counts are fallbacks rather than universal minimums. Focused, incremental, and metadata-only turns may choose a smaller justified scope and stop at the evidence stage needed by the current question; selected conference years still require complete-catalog acquisition before topical filtering.
- Multi-turn research uses immutable parent/child runs plus shared caches. A new direction links prior artifacts and searches only its evidence gap instead of overwriting the prior result or rerunning unrelated stages.
- HTTP 429/503 retries and cross-process throttling are reliability enhancements requested after observed failures; the original Find arXiv path recorded rate limiting and stopped with partial results rather than performing this retry loop.
- Non-semantic integrity enhancements bind receipts to full-text/source-abstract/read hashes, require a complete source-sentence translation map, reject only exactly duplicated long sections across distinct papers, and version reading caches. These strengthen validation without replacing the original Reading content rules; no fuzzy similarity threshold is used.

## Certification rule

Do not claim migration parity merely because tests pass. Certification requires structural tests, representative live-source receipts, cache-reuse/revision tests, and a manual comparison against the authoritative original functions named in the repository history. Any known deviation must be reported explicitly.

## Upstream comparison on 2026-07-22

- Compared against TASTE commit `d8b9a8fcfb50f48a873255e7aec894ce248d355a` (`fix read`). Its unknown-host change from one global `generic` service to `host_<hostname>` is retained and generalized here: unrelated services/hosts have independent process-safe slot pools, and each channel can declare capacity greater than one through `RECOMMEND_PAPERS_HTTP_CONCURRENCY`. TASTE's serial shared gate is copied for OpenReview by default and generalized for other sources according to observed safe capacity. OpenReview's official client and direct HTTP routes share one persisted cooldown, its override is hard-capped at three, and TASTE's one-worker cooldown-expiry batch requeue is migrated for transient full-text failures.
- Migrated the stronger front-matter title-window plus author-family PDF identity check. This replaces the weaker behavior that could accept a wrong PDF merely because most title tokens appeared later in its body or references.
- Migrated OpenReview's `originally_submitted_PDF` attachment fallback after `pdf`.
- Retained the explicit one-paper prompt and one-time minimal quality-repair policy as external Claude subprocesses. At the user's direction, external Claude scheduling is a hard-capped 16-process pipeline that immediately refills completed slots; when Claude is unavailable or explicitly disabled, the separate fast path uses exactly three Codex batch subagents and intentionally bypasses per-paper artifacts.
