# Standalone service contract

## Boundaries

The backend is entirely under this skill's `scripts/recommend_service/`. It provides crawling, normalization, caching, acquisition, validation, and resumable state management. It reads no project state and writes no Git repository by default.

Resolve the service from the installed `SKILL.md` directory. Invoke it with one consistently selected Python command prefix:

```text
<python-prefix> <service> doctor
```

The prefix may be an executable or a launcher with separate version arguments. It may target any Python 3.10+ environment with `requirements.txt` installed. Continue only if `doctor` reports `status: ok`, `python_version_supported: true`, and `standalone: true`.

## Storage

- Persistent intermediate state: `${XDG_STATE_HOME:-~/.local/state}/taste/recommend-papers/`
- Runs: `${XDG_STATE_HOME:-~/.local/state}/taste/recommend-papers/runs/<UTC-run-id>/`
- Reusable cache: `${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers/`
- Overrides: absolute `RECOMMEND_PAPERS_STATE_DIR`, `RECOMMEND_PAPERS_CACHE_DIR`; legacy `RECOMMEND_PAPERS_DATA_DIR` remains an alias for the state root
- Explicit Git export escape hatch: `RECOMMEND_PAPERS_ALLOW_GIT_WRITES=1`

One run contains:

```text
run.json
plan.json
source_receipts/<index>.json
metadata.json
metadata_scores.raw.json
metadata_scores.json
shortlist.json
full_text_results.json
papers/<index>/acquisition.json
evidence_cards.jsonl
evidence_cards.validated.json
final_ranking.json
recommendations.md
```

`run.json` tracks the active stage and counts. Only `complete` sets its status to `complete`.

## Sources

Supported metadata types:

- `arxiv`: Atom API; requires concrete `start_date` and `end_date`. It forbids result limits. The default category set is `cs.AI`, `cs.LG`, `stat.ML`, `cs.CL`, `cs.CV`, `cs.IR`, `cs.RO`, `eess.SY`, `cs.MA`, and `cs.NE`; explicit user categories override it. `cs.CL` and `cs.CV` cover NLP and vision, while `cs.RO` plus `eess.SY` cover embodied-intelligence robotics and control. The backend performs exhaustive server-side category/date pagination and stores complete category/day shards. Task keywords never narrow this base crawl.
- `biorxiv`: official bioRxiv API; requires concrete dates and forbids result limits. Acquisition is exhaustive and all-category; task queries/categories are recorded but ignored until Codex metadata scoring.
- `venue`: `venue_id` from `catalog`, or a custom DBLP/OpenReview venue; requires exactly one year and `complete_catalog: true`. Limits, topic queries, categories, tracks, and samples are forbidden at acquisition time. OpenReview paginates until its final page; DBLP reads the complete venue-volume XML rather than its capped search API. Only after this complete pool is cached may Codex rank all papers from titles and abstracts and select the default 100.
- `journal`, `nature`, `science`: Crossref, optionally filtered by journal names and queries.

Baseline IDs are `neurips`, `iclr`, and `icml`. A custom venue can use:

```json
{"type":"venue","adapter":"dblp","venue":"CVPR","years":[2025],"limit":5000}
```

or:

```json
{"type":"venue","adapter":"openreview","venue":"ICLR","openreview_venue_id":"ICLR.cc/2026/Conference","years":[2026],"complete_catalog":true}
```

Every plan must consider NeurIPS/NIPS, ICLR, ICML, and arXiv in `channel_decisions`. Venue sources require one resolved year; preprint sources require concrete dates.

Every plan declares `request_scope.user_specified_time`, `request_scope.user_specified_channels`, and `request_scope.as_of_date`. When both user flags are false, validation requires NeurIPS/NIPS, ICLR, and ICML at their respective latest usable complete-metadata years, exactly one arXiv source over the inclusive trailing six calendar months, and one to three additional topic-adaptive sources. Adaptive conferences use their latest usable year; adaptive non-conference streams use the identical six-month start/end dates.

Only one metadata crawl may write a run at a time. Cross-process locks must retain this behavior on Windows, macOS, and Linux. A live command session must be resumed until its final exit status, not treated as termination. bioRxiv receipts explicitly expose `server_total`, `server_total_scanned`, `exhausted`, `truncated`, `next_cursor`, and `exhaustion_proof`; truncated coverage blocks shortlisting. Shortlist, full-text, final-ranking, and completion artifacts are bound to upstream fingerprints and stale artifacts are rejected after any metadata change.

The shared HTTP client serializes each service across threads and Codex processes, preserves persisted cooldown state, and honors `Retry-After`. arXiv uses a 5.1-second minimum interval and up to eight attempts for 429/5xx responses; other services retain their configured retry policy. These responses remain internal transient events until retry exhaustion; Codex must not present an intermediate attempt as a confirmed source failure.

## Metadata cache

The cache root has exactly two visible children: `metadata` and `fulltext`. The only authoritative metadata root is `${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers/metadata` (or `<RECOMMEND_PAPERS_CACHE_DIR>/metadata`). Every metadata cache is directly under a channel directory. Conferences use `metadata/conference/<venue>/<year>.json`; only a complete-catalog payload with an exhaustion proof may occupy that canonical year file. Nature, Science, and other journal streams use `metadata/<channel>/<start>_<end>_<spec-fingerprint>.json`. arXiv uses `metadata/arxiv/<category>/<YYYY-MM-DD>.json`; bioRxiv uses `metadata/biorxiv/<YYYY-MM-DD>.json`. Empty but proven-complete historical days are cached too.

`migrate-metadata-cache` canonicalizes usable channel caches, selects the newest proven-complete copy for each conference/year, and removes duplicate, limited, unclassified, imported, and legacy Finding artifacts. Old query/limit-based arXiv hashes are removed because only category/day shards are authoritative. HTTP throttling state moves into hidden files in the owning channel rather than a third top-level cache directory. Metadata runs invoke the same migration automatically before crawling, preventing future boundary drift.

`reuse` uses fresh closed-day entries, `refresh` refetches, and `only` forbids network. Default freshness is seven days and may be set with `metadata_cache_max_age_days`. A shard is reusable only when `shard_date < local_today`, `complete=true`, and `provisional` is not true. The current calendar day is necessarily open: even after the API reaches its current `totalResults`, its JSON is written `complete=false`, `provisional=true` and is fetched again on every subsequent crawl. Once that date becomes historical, the next crawl refetches it and may publish it as complete.

Missing/stale arXiv shards are grouped by category and natural month. Every successfully parsed API page is persisted under the non-cache runtime path `<data-root>/state/arxiv-staging/<category>/<range>/<offset>.json`, so retries resume at the first missing page. Each month/range is paginated until its own OpenSearch `totalResults` is reached, then partitioned into daily shards, and its staging directory is deleted immediately. Failed/incomplete ranges retain staging; successful ranges never leave staging under `metadata`. Month segmentation avoids arXiv's 10,000-offset deep-pagination failure.

Missing/stale bioRxiv days are grouped by natural month and crawled without keyword, category, or count restrictions. Every parsed cursor page is persisted under `<data-root>/state/biorxiv-staging/<range>/<cursor>.json`; a range is published into daily shards only after the cursor reaches the official server total (or a valid empty terminal page), after which its staging directory is deleted. Failed ranges retain staging. Today is always `complete=false, provisional=true` and is refetched by every later crawl. The preceding three closed calendar days are reused for at most 24 hours so delayed indexing or metadata corrections are reconciled; older proven-complete days are reused indefinitely unless `refresh` is requested. Raw daily shards preserve DOI versions, while cross-day task output deterministically selects the highest numeric version, then latest date and more complete abstract.

## Metadata scoring artifact

Codex must score every identity in `metadata.json`:

```json
{
  "scores": [{
    "identity": "doi:10.x/example",
    "metadata_score": 87,
    "components": {
      "topic_fit": 45,
      "transferability_potential": 26,
      "abstract_specificity": 16
    },
    "reason": "...",
    "uncertainty": "..."
  }]
}
```

`shortlist` validates complete coverage and arithmetic, sorts deterministically, and selects the requested target (default 100). Metadata scoring is deliberately limited to title-and-abstract scientific evidence. Only selected papers are passed to full-text acquisition, dedicated per-paper Codex subagents, and final full-text scoring.

## Full-text cache and resume

Candidate order is the official OpenReview attachment API (authenticated when credentials exist, otherwise anonymous), metadata PDF, arXiv/OpenReview public PDF fallback, OpenAlex/Unpaywall OA PDF, same-paper HTML, and Europe PMC XML. OpenReview attachment retrieval uses `openreview-py`; receipts distinguish authentication failure, missing attachment, public-endpoint `challenge_403`, parse failure, and identity mismatch. PDF text is extracted with PyMuPDF. A result is ready only when it has at least 1200 characters, body-section markers, and title/author identity evidence.

Successful artifacts are cached under `cache/fulltext/<identity-hash>/`. Identity prefers DOI, arXiv ID, OpenReview ID, or PMCID; title fallback includes first author and year. Run resume checks identity, not only list index. Failed acquisition receipts remain in the run.

## Single-paper read artifacts

Run `prepare-reads` after full-text acquisition. Each ready paper receives `papers/<index>/read.md`; `reading_queue.json` lists restored and pending artifacts. A cached read is restored only when its stored full-text SHA-256 equals the current text.

Each pending read must contain the exact title as H1, two metadata bullets (`来源`, `论文链接`), and these H2 sections in order: `摘要`, `动机与核心创新`, `方法`, `实验结果`, `优缺点总结`. The abstract is substantive Chinese, must not copy long English prose, and must preserve a supplied `abstract_zh` verbatim. Motivation/innovation includes `动机：` and `核心创新：` with 200–250 Chinese characters. Method uses 300–400 Chinese characters plus a relevant LaTeX formula and explanation. Experiments use at most 150 Chinese characters. Strengths/limitations use at most 100 Chinese characters. LaTeX commands outside math delimiters and malformed delimiters are rejected.

Each pending queue item supplies `receipt_path`, the source abstract, and the full-text SHA-256. A dedicated Codex subagent writes `read.md` plus `read_receipt.json`. Matching original Reading semantics, the receipt must report `status=complete`, `subagent_deep_read=true`, and `deep_read_audit.mode=dedicated_codex_subagent`, `subagent_used=true`, the exact `text_path`, `evidence_chars` at least the extracted full-text character count, the exact `article_markdown_path`, and `article_markdown_written=true`. Integrity enhancements additionally bind full-text/source-abstract/read SHA-256 values and provide one `{source_sha256, translation_zh}` entry per original abstract sentence.

The main Codex dispatches exactly one pending paper per dedicated subagent, fills the maximum number of subagent slots exposed by its current runtime, and immediately refills completed slots until the queue is empty. A fixed agent count is intentionally not hard-coded because runtime concurrency limits vary. All dispatched agents must finish and yield distinct validated artifacts before final ranking.

Minimal receipt shape:

```json
{
  "status": "complete",
  "paper_id": "doi:10.x/example",
  "source": "ICLR 2026",
  "title": "Exact paper title",
  "run_dir": "/absolute/run/path",
  "article_markdown_path": "/absolute/run/path/papers/0001/read.md",
  "subagent_deep_read": true,
  "abstract_sentence_map": [{"source_sha256": "...", "translation_zh": "..."}],
  "deep_read_audit": {
    "mode": "dedicated_codex_subagent",
    "subagent_used": true,
    "full_text_sha256": "...",
    "source_abstract_sha256": "...",
    "read_sha256": "...",
    "text_path": "/absolute/cache/path/full_text.txt",
    "evidence_chars": 12345,
    "article_markdown_path": "/absolute/run/path/papers/0001/read.md",
    "article_markdown_written": true
  }
}
```

`validate-reads` rejects malformed, too-short, non-Chinese, formula-free, wrong-title, unaudited, or stale-contract artifacts. On success it writes `read_artifacts.json`, aggregates all reads to `<run-dir>/read.md`, and caches each read and receipt under `cache/fulltext/<identity-hash>/reading/` with its full-text fingerprint and reading-contract version. A version change invalidates old reading caches without deleting full-text caches.

## Evidence card

Write one JSON object per line for every `full_text_available: true` item:

```json
{
  "identity": "doi:10.x/example",
  "title": "Paper title",
  "full_text_path": "/absolute/cache/path/full_text.txt",
  "read_path": "/absolute/run/path/papers/0001/read.md",
  "summary": "...",
  "decisive_evidence": ["..."],
  "limitations": ["..."],
  "borrowable_elements": ["..."],
  "confidence": "high",
  "match_score": 9.2,
  "transferability_score": 8.7,
  "final_score": 17.9
}
```

`finalize` rejects missing/duplicate/foreign cards, missing validated reads, incorrect full-text/read paths, missing scientific fields, and incorrect arithmetic. It creates the canonical final ranking and top-N set.
