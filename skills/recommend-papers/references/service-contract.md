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

`run.json` tracks the active stage and counts. State updates are atomic and cross-process locked. Downstream commands require an existing initialized run, reject a mistyped path instead of silently creating it, and reject mutation after completion. `complete` closes recommendation workflows; `finish-stage` closes an intentional partial workflow after validating its selected endpoint.

## Multi-turn and partial workflows

`init-run` accepts optional `--parent-run-dir`, `--mode`, and `--question`. A child run records links to available parent artifacts in `continuation.json` but never copies or mutates them. If the completed parent's plan carries a conversation-level no-Claude lock, the continuation also carries those exact inherited workflow settings and metadata validation rejects a child plan that silently drops them. Shared caches provide cross-run reuse. Use the same run for a same-scope follow-up while it remains active; create a child for a materially different question, corpus, date window, or ranking objective.

Every metadata plan may include `workflow.mode` (`comprehensive`, `focused`, `incremental`, `metadata_only`), `research_question`, `rationale`, `stop_after`, `reading_preference`, `shortlist_target`, and `final_target`. A missing workflow preserves the prior behavior: no user-specified time/channels implies comprehensive defaults. A non-comprehensive workflow requires a concrete question and rationale, then may choose a narrower source/date/count configuration. This is an explicit scoped claim, not exhaustive coverage. When `codex_fast` comes from an explicit user refusal of Claude, normalization adds `reading_preference_scope: conversation` and `conversation_reading_preference_locked: true` unless the user explicitly limited the choice to the current turn. Every later plan in the same Codex conversation must copy this lock until the user explicitly re-enables Claude.

The backend stages are composable but cannot exceed `workflow.stop_after`. `finish-stage` validates and closes exactly the planned endpoint at metadata, shortlist, fulltext, or reading. `finalize`/`complete` are accepted only for a recommendation endpoint. Defaults are used only for unspecified settings and are never enforced as minimum paper counts. A child run may name only a completed immutable parent; an active run must be continued directly.

## Sources

Supported metadata types:

- `arxiv`: Atom API; requires concrete `start_date` and `end_date`. It forbids result limits. The default category set is `cs.AI`, `cs.LG`, `stat.ML`, `cs.CL`, `cs.CV`, `cs.IR`, `cs.RO`, `eess.SY`, `cs.MA`, and `cs.NE`; explicit user categories override it. `cs.CL` and `cs.CV` cover NLP and vision, while `cs.RO` plus `eess.SY` cover embodied-intelligence robotics and control. The backend performs exhaustive server-side category/date pagination and stores complete category/day shards. Task keywords never narrow this base crawl.
- `biorxiv`: official bioRxiv API; requires concrete dates and forbids result limits. Acquisition is exhaustive and all-category; task queries/categories are recorded but ignored until Codex metadata scoring.
- `venue`: `venue_id` from `catalog`, or a custom DBLP/OpenReview venue; requires exactly one year and `complete_catalog: true`. Limits, topic queries, categories, tracks, and samples are forbidden at acquisition time. The built-in catalog supports the complete 14-venue TASTE set: NeurIPS/NIPS, ICLR, ICML, SIGKDD/KDD, SIGIR, CIKM, AAAI, ICCV, WWW, CVPR, ACL, IJCAI, ECCV, and EMNLP. It uses OpenReview, CVF Open Access, ACL Anthology, AAAI OJS, IJCAI proceedings, ECVA, or ACM/indexed enrichment according to the original source policy. DBLP title records for a priority venue are seeds/cross-checks only and can never prove full metadata without abstracts. Only after the complete title-and-abstract pool is cached may Codex rank all papers and select the default 100.
- `journal`, `nature`, `science`: Crossref, optionally filtered by journal names and queries.

Baseline IDs are `neurips`, `iclr`, and `icml`. A custom venue can use:

```json
{"type":"venue","adapter":"dblp","venue":"CVPR","years":[2025],"complete_catalog":true}
```

or:

```json
{"type":"venue","adapter":"openreview","venue":"ICLR","openreview_venue_id":"ICLR.cc/2026/Conference","years":[2026],"complete_catalog":true}
```

Every comprehensive plan must consider NeurIPS/NIPS, ICLR, ICML, and arXiv in `channel_decisions`; focused and incremental plans record only channels actually considered for their evidence gap. Venue sources require one resolved year; preprint sources require concrete dates. Resolution uses a run-aware exact-year probe through every configured live adapter. The internal limit of three constrains diagnostic fetching/enrichment; the command exposes no sampled papers, while its same-run receipt stores them only under `diagnostic_samples` with `research_output=false` and `complete_catalog=false`. Formal venue acquisition requires a matching `probe_available` receipt and performs a separate full-catalog fetch; a latest-year claim additionally requires the probe to have started at `request_scope.as_of_date`'s year. Each adapter probe has a shared 30-second wall-clock request budget by default: nested detail enrichment inherits the remaining deadline and cannot reset it. `RECOMMEND_PAPERS_PROBE_WALL_TIMEOUT_SECONDS` may raise that budget to at most 300 seconds for a known-slow live archive without changing the three-record diagnostic limit. An exact-year official archive 404, absent official proceedings index, or absent OJS issue is recorded as authoritative empty rather than a crawler error. Only authoritative empty results across all configured routes permit checking an older year; transient failures and cooldowns return unresolved. Release calendars are informational and never suppress a real request.

Every plan declares `request_scope.user_specified_time`, `request_scope.user_specified_channels`, and `request_scope.as_of_date`. In comprehensive mode, when both user flags are false, validation requires NeurIPS/NIPS, ICLR, and ICML at their respective latest usable complete-metadata years, exactly one arXiv source over the inclusive trailing six calendar months, and one to three additional topic-adaptive sources. Adaptive conferences use their latest usable year; adaptive non-conference streams use the identical six-month start/end dates. Focused, incremental, and metadata-only modes may narrow this scope when their recorded question and rationale justify it.

Only one metadata crawl may write a run at a time. Within that crawl, independent sources use a bounded six-worker pool by default (`RECOMMEND_PAPERS_METADATA_SOURCE_WORKERS`, hard-capped at eight); per-service HTTP slots, spacing, and cooldowns still serialize or bound DBLP, OpenReview, ACM, and each official host independently. Cross-process locks must retain this behavior on Windows, macOS, and Linux. A live command session must be resumed until its final exit status, not treated as termination. bioRxiv receipts explicitly expose `server_total`, `server_total_scanned`, `exhausted`, `truncated`, `next_cursor`, and `exhaustion_proof`; truncated coverage blocks shortlisting. The normalized plan becomes immutable when metadata is produced. Shortlist, full-text, reading, final-ranking, and completion artifacts are bound to plan and upstream fingerprints; stale artifacts are rejected after any plan, metadata, shortlist, or full-text change.

Stage commands keep large scientific arrays in their canonical run artifacts and return compact CLI receipts. In particular, `metadata` never prints the full paper corpus to stdout; it returns status/counts, `metadata_path`, and compact source summaries. Random-shortlist and full-text commands likewise return paths and aggregate coverage while retaining selected papers and per-route attempts in `shortlist.json` and `full_text_results.json`.

The shared HTTP client gives every API or official host an independent cross-thread and cross-process slot pool, minimum start interval, and persisted cooldown while honoring `Retry-After`. It never sleeps while holding the shared state-file lock. Unrelated channels never share a slot or cooldown. The global full-text worker count bounds aggregate downloads; per-channel capacity independently limits one source according to observed behavior. `RECOMMEND_PAPERS_HTTP_CONCURRENCY` may provide a JSON map of service/host overrides. Authoritative arXiv crawling uses one slot, a 5.1-second minimum interval, and up to eight attempts for 429/5xx responses; lightweight probes and optional venue enrichment override this with a short request budget and defer rather than consuming the full 22.5-minute worst-case backoff. Challenge-prone ACM also defaults to one slot. DBLP uses one slot and a one-second start interval; its three official hosts share state, transient network/5xx failures rotate mirrors, and a `Retry-After` longer than `DBLP_MAX_RETRY_AFTER_WAIT_SEC` (default 10 seconds) defers the request rather than blocking an interactive run. OpenReview defaults to one slot and has a non-overridable ceiling of three. All openreview-py client construction/login, venue-note pagination, attachment calls, and direct OpenReview HTTP/PDF/HTML requests enter the same process-safe `openreview` gate and write 429/403 cooldowns to the same state. Other services retain their configured retry policy, including bounded connection/TLS retries. These responses remain internal transient events until the operation's configured retry budget ends; Codex must not present an intermediate attempt as confirmed source absence.

ACM-family metadata first applies batch indexes and exact-title local arXiv cache matches. Remaining per-paper fallbacks are checkpointed after every row. Remote arXiv exact-title fallback is capped at 12 actual requests per formal invocation by default (`RECOMMEND_PAPERS_ACM_ARXIV_FALLBACK_BUDGET` may override it); a later resume continues from saved papers instead of repeating completed work. Exhausting this optional-request budget leaves the formal catalog incomplete—it never converts missing abstracts into a successful cache.

## Metadata cache

The cache root has exactly two visible children: `metadata` and `fulltext`. The only authoritative metadata root is `${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers/metadata` (or `<RECOMMEND_PAPERS_CACHE_DIR>/metadata`). Every metadata cache is directly under a channel directory. Conferences use `metadata/conference/<venue>/<year>.json`; only a complete-catalog payload with an exhaustion proof may occupy that canonical year file. Nature, Science, and other journal streams use `metadata/<channel>/<start>_<end>_<spec-fingerprint>.json`. arXiv uses `metadata/arxiv/<category>/<YYYY-MM-DD>.json`; bioRxiv uses `metadata/biorxiv/<YYYY-MM-DD>.json`. Empty but proven-complete historical days are cached too.

`migrate-metadata-cache` canonicalizes usable channel caches, selects the newest proven-complete copy for each conference/year, and removes duplicate, limited, unclassified, imported, and legacy Finding artifacts. Old query/limit-based arXiv hashes are removed because only category/day shards are authoritative. HTTP throttling state moves into hidden files in the owning channel rather than a third top-level cache directory. Metadata runs invoke the same migration automatically before crawling, preventing future boundary drift.

When the user explicitly requests deletion followed by a fresh conference crawl, set `RECOMMEND_PAPERS_DISABLE_RUN_CACHE_RECOVERY=1` for migration and metadata. This prevents historical run receipts from resurrecting a deliberately purged conference cache; combine it with `cache_policy: refresh` to force the live adapter.

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

`shortlist` validates complete coverage and arithmetic, sorts deterministically, and selects the requested target (default 100). Metadata scoring is deliberately limited to title-and-abstract scientific evidence. Only selected papers are passed to full-text acquisition and the selected reading mode.

For an explicit request to sample papers uniformly at random within each selected conference, use `random-venue-shortlist`. It requires complete formal metadata, a venue-only plan with one resolved year per venue, and a plan target equal to `--per-venue × venue_count`. Sampling is without replacement within each venue; the receipt persists the random seed, population size, selected identities, and per-venue counts. Probe diagnostics are never eligible input.

If that explicit task requires a successful per-venue PDF count rather than merely attempted downloads, `replace-failed-random-venue` preserves every ready paper and draws only the missing count from previously untried papers in the same venue/year. It records a deterministic replacement round and cumulative attempted identities. Rerunning `fulltext` reuses validated successful caches. Repeat only until every stratum reaches its requested ready count or the same-venue population is genuinely exhausted.

Priority-venue recommendation and topical-scoring workflows still require a real abstract for every cached row. A venue source may set `require_complete_abstracts: false` only for an explicit catalog-core workflow that does not score or recommend from abstracts, such as per-venue random sampling followed by full-text acquisition. The adapter must still prove catalog exhaustion and the core audit still requires unique identities plus at least 95% author and link coverage. Its receipt records abstract coverage and gaps; a later abstract-scoring workflow cannot reuse that cache as full-abstract metadata.

When downstream work requires at least a known number of papers per conference, set `minimum_catalog_records` on every venue source. Formal acquisition fails below that count even if API pagination is exhausted: exhaustion proves that all currently visible rows were fetched, not that an early/partially public accepted-paper catalog is the whole conference. Resolve the next real edition with a new probe receipt rather than sampling from a visibly undersized catalog.

## Full-text cache and resume

Candidate order is the official OpenReview attachment API (authenticated when credentials exist, otherwise anonymous), deterministic official conference PDF, metadata PDF, arXiv/OpenReview public PDF fallback, DOI-based OpenAlex/Unpaywall OA PDF, exact-title Semantic Scholar/HAL/arXiv public copy, same-paper HTML, and Europe PMC XML. The OpenReview client tries `pdf` and then `originally_submitted_PDF`. If every official OpenReview route fails, the latest TASTE ChatPaper cache route may be tried: its page must contain the exact expected OpenReview note id, its requests share a one-slot cross-process gate with a 10.1-second start interval, and the downloaded PDF still passes the normal title/author/body identity gate. Optional OA indexes are queried only after supplied and official locators fail; an active optional-service cooldown is recorded and skipped. Exact-title lookup requires equal normalized titles and every resulting PDF still passes title/author and paper-body validation. PDF identity is checked against title-like windows in the first-page front matter plus author-family overlap, rather than accepting title words found anywhere in the body or references. OpenReview receipts distinguish authentication failure, missing attachment, public-endpoint `challenge_403`, parse failure, and identity mismatch. PDF text is extracted with PyMuPDF. A result is ready only when it has at least 1200 characters, body-section markers, and title/author identity evidence.

Successful artifacts are cached under `cache/fulltext/<identity-hash>/`. Identity prefers DOI, arXiv ID, OpenReview ID, or PMCID; title fallback includes first author and year. Both cache keys and run-resume receipts bind the acquisition-contract version, so an identity-validation upgrade cannot reuse an artifact accepted under weaker rules. Run resume checks identity, not only list index. Failed acquisition receipts remain in the run. A 429, active shared cooldown, or 403/challenge produces `temporarily_unavailable` with `retryable_after_cooldown: true`, not a permanent absence claim. After the parallel acquisition pass, the coordinator waits up to `RECOMMEND_PAPERS_COOLDOWN_REQUEUE_WAIT_CAP_SECONDS` (180 seconds by default) for the relevant service cooldown and retries every deferred paper exactly once, serially. The run-level `cooldown_requeue` receipt records eligible, attempted, recovered, skipped, and waited counts.

One full-text acquisition worker never waits indefinitely for either a persisted cooldown or an occupied cross-process slot. Its per-wait ceiling defaults to 120 seconds (`RECOMMEND_PAPERS_FULLTEXT_RETRY_AFTER_WAIT_CAP_SECONDS`) and its aggregate request wall budget defaults to 600 seconds (`RECOMMEND_PAPERS_FULLTEXT_REQUEST_WALL_BUDGET_SECONDS`). Exceeding either produces `ServiceRequestDeferred`, which is classified as temporary and enters the same cooldown requeue. ECCV archive URLs deterministically derive the zero-padded official ECVA PDF; ECCV virtual pages may discover the main PDF but must reject supplement, poster, and slide links.

Before every concrete PDF candidate request, acquisition checks that candidate service's persisted cooldown. A cooling service is recorded as `skipped_persisted_cooldown` and the worker immediately tries the next same-paper route; it never joins a queued line that wakes one worker at a time only to renew the same ACM/OpenReview challenge cooldown. This preflight is in addition to, not a replacement for, the shared slot and retry governor.

The same preflight applies before HTML fallback. ACM DOI landing URLs (`doi.org/10.1145/...`) are attributed to the `acm` service before following redirects, so an ACM challenge cannot hide behind a separate DOI-host queue and serialize every remaining worker.

The shared request slot itself also caps ACM cooldown/slot waiting at five seconds (`ACM_MAX_COOLDOWN_WAIT_SEC`), taking the smaller of this service ceiling and any caller budget. This closes the atomic race where several workers pass an outer zero-cooldown preflight before the first response records a 403; queued followers defer instead of sleeping and renewing that challenge one by one.

Optional per-paper OpenAlex, Unpaywall, Semantic Scholar, HAL, and arXiv discovery calls additionally use a nested short wait/wall budget. This closes the race where many workers all observe zero cooldown just before the first 429 and then queue behind the newly persisted cooldown. Deferral is recorded as an optional-route failure and acquisition continues with other same-paper routes.

## Reading modes

External Claude is the default. `doctor` reports the resolved Claude CLI without making it a required Python dependency. After `prepare-reads`, `claude-reads` gives every pending paper a fresh external Claude process through a bounded pipeline. The hard concurrency ceiling is 16; `--workers` may lower it but cannot raise it. Completion of any process immediately frees a slot for the next paper, so 100 papers run as a continuously refilled 16-slot queue rather than 100 resident processes. Each command uses `claude -p --permission-mode bypassPermissions --output-format json`, disables `Agent`, `Task`, and worktree tools, and receives only one paper prompt. This pool is independent of Codex subagent capacity.

When Claude cannot be resolved, `claude-reads` returns and persists `status: claude_unavailable` and performs no reading. When that occurs—or when the plan records that the user explicitly forbids Claude—`prepare-fast-read-batches` writes exactly three balanced manifests. The backend rejects this command without one of those two proofs and rejects Claude commands when the plan selects the fast branch. Exactly three Codex subagents directly read their entire assigned batches and return batch evidence. This fast branch bypasses all single-paper Markdown, receipt, translation-map, repair, and read-cache machinery. Reading completion validates all three result files, exact assignments and paths, scientific fields, score arithmetic, and the current full-text fingerprint. `finalize` repeats the evidence and fingerprint gates. No other Claude failure automatically authorizes this lower-audit fallback.

## Default Claude single-paper artifacts

Run `prepare-reads` after full-text acquisition. Each ready paper receives `papers/<index>/read.md`; `reading_queue.json` lists restored and pending artifacts. Every pending item also has a deterministic `papers/<index>/read_prompt.md` and exposes its absolute `prompt_path`, so concurrently dispatched agents receive identical whole-text, output-scope, Markdown, translation, receipt, and hash requirements. A cached read is restored only when its stored full-text SHA-256 equals the current text. Before reuse or validation, deterministic title/source/link front matter is normalized to current paper metadata and relocated artifact paths/hashes are rebound in the receipt; scientific sections are never modified by this repair.

Each pending read must contain the exact title as H1, two metadata bullets (`来源`, `论文链接`), and these H2 sections in order: `摘要`, `动机与核心创新`, `方法`, `实验结果`, `优缺点总结`. The abstract is substantive Chinese, must not copy long English prose, and must preserve a supplied `abstract_zh` verbatim. Motivation/innovation includes `动机：` and `核心创新：` with 200–250 Chinese characters. Method uses 300–400 Chinese characters plus a relevant LaTeX formula and explanation. Experiments use at most 150 Chinese characters. Strengths/limitations use at most 100 Chinese characters. LaTeX commands outside math delimiters and malformed delimiters are rejected.

Each pending queue item is self-contained for one paper: identity, title, authors, source/date, canonical paper URL, the accepted remote PDF URL, local PDF path when retained, run directory, exact `full_text_path`, output `read_path`/`receipt_path`, source abstract, and full-text/source-abstract SHA-256 values. One external Claude process receives that item and writes `read.md` plus `read_receipt.json`. Matching original Reading semantics, the receipt must report `status=complete`, `subagent_deep_read=true`, and `deep_read_audit.mode=dedicated_claude_subagent`, `subagent_used=true`, the exact `text_path`, `evidence_chars` at least the extracted full-text character count, the exact `article_markdown_path`, and `article_markdown_written=true`. Integrity enhancements additionally bind full-text/source-abstract/read SHA-256 values and provide one `{source_sha256, translation_zh}` entry per original abstract sentence.

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
    "mode": "dedicated_claude_subagent",
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

`validate-reads` rejects malformed, too-short, non-Chinese, formula-free, wrong-title, unaudited, or stale-contract artifacts. `claude-reads --only-failed` gives each failed paper one fresh external Claude process for one minimal repair pass through the same maximum-16 pipeline. On success the validator writes `read_artifacts.json`, aggregates all reads to `<run-dir>/read.md`, and caches each read and receipt under `cache/fulltext/<identity-hash>/reading/` with its full-text fingerprint and reading-contract version. A version change invalidates old reading caches without deleting full-text caches.

## Evidence card

Write one JSON object per line for every `full_text_available: true` item. Default Claude mode uses `read_path`; the fast fallback replaces it with the assigned `batch_id`:

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

`finalize` rejects missing/duplicate/foreign cards, incorrect full-text paths, missing scientific fields, and incorrect arithmetic. In default mode it additionally requires the validated `read_path`; in fast mode it instead requires the exact assignment from one of the three manifests. It creates the canonical final ranking and top-N set.
