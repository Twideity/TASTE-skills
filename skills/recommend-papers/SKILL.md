---
name: recommend-papers
description: Find, acquire, deeply read, rank, and recommend scholarly papers for any research request. Use when the user explicitly invokes $recommend-papers or asks Codex for literature discovery, paper recommendations, related-work search, evidence mapping, method scouting, or research-paper comparison. Codex chooses sources, topics, queries, and date windows, while the bundled backend crawls and caches metadata and full text, maintains resumable state, and validates the complete configurable shortlist-to-recommendation workflow.
---

# Recommend papers

Perform all research judgment within Codex. Use dedicated Codex subagents for per-paper deep reading when subagents are available, while the main Codex plans, verifies receipts, compares papers, performs final scoring, and synthesizes the answer. Use the bundled backend for crawling, normalization, identity validation, caching, artifact validation, and run state.

## Runtime

The backend must work on Windows, macOS, and Linux. Do not replace its cross-platform Python entry points or locks with platform-specific shell commands or operating-system APIs.

Resolve `SERVICE` by taking the absolute source locator of this `SKILL.md` and appending `scripts/paper_service.py`. Resolve only the service bundled with this installed skill; do not derive it from the current working directory or search the workspace for an alternative implementation.

Resolve one Python command prefix and reuse it for the entire run. Prefer the installer-managed interpreter at the platform data location (`~/Library/Application Support/TASTE/recommend-papers/venv/bin/python` on macOS, `${XDG_DATA_HOME:-~/.local/share}/taste/recommend-papers/venv/bin/python` on Linux, or `%LOCALAPPDATA%\TASTE\recommend-papers\venv\Scripts\python.exe` on Windows). Otherwise consider: (1) an absolute `RECOMMEND_PAPERS_PYTHON` executable supplied by the user; (2) the active Python 3.10+ environment; (3) `<SKILL_DIR>/../../.venv/bin/python` or its Windows counterpart when present; (4) standard versioned Python commands; (5) an available Conda environment. A prefix may be one executable path or a launcher plus version arguments such as `py -3.12`. For each candidate, run `paper_service.py doctor`:

- Select it immediately when `status` is `ok`.
- When it returns `error_type: runtime_setup` with `missing_module`, select that Python 3.10+ candidate. Install the reported `requirements` with the exact same command prefix only when it belongs to a user-writable isolated environment; otherwise create an isolated venv first. Then rerun `doctor`.
- Reject candidates that report an unsupported Python version. Stop and report non-setup failures such as unwritable storage instead of hiding them by changing Python.

The environment manager and environment name are not requirements. Never switch Python command prefixes midway through a run. Use native invocation syntax: `"$PYTHON" "$SERVICE" ...` for a single executable in POSIX shells, `& $Python $Service ...` in PowerShell, or the equivalent argument-vector execution provided by the current Codex tool. Do not store a launcher and its arguments in one quoted string.

OpenReview always uses the official `openreview-py` client. Prefer authenticated access when both credentials are present in the private `read.env` beside this file; otherwise use the anonymous official client automatically. The user may edit `read.env`, provide `OPENREVIEW_USERNAME` and `OPENREVIEW_PASSWORD` in the process environment, or set `RECOMMEND_PAPERS_OPENREVIEW_ENV_FILE` to an absolute alternative. Never print credentials, copy them into a run, or include them in recommendations.

Keep the skill directory immutable during use. Follow the XDG base-directory split for every runtime write:

- Intermediate runs, resumable staging, receipts, locks, and HTTP state: `${XDG_STATE_HOME:-~/.local/state}/taste/recommend-papers`
- Reusable metadata, full text, and validated single-paper reads: `${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers`

`RECOMMEND_PAPERS_STATE_DIR` and `RECOMMEND_PAPERS_CACHE_DIR` may override the two application roots with absolute paths. `RECOMMEND_PAPERS_DATA_DIR` remains a backward-compatible alias for the state root only. Never store task runs or reusable article data inside the skill, `CODEX_HOME`, the current working directory, or a Git repository.

The cache root must contain exactly two directories: `metadata` for discovery metadata and `fulltext` for acquired full text plus validated single-paper reading artifacts. Metadata is organized directly by channel: `arxiv/<category>/<day>.json`, `biorxiv/`, `nature/`, `science/`, `journal/`, and `conference/<venue>/<year>.json`. Never create `sources`, `imports`, `finding-runtime`, top-level `http-state`, or hidden state inside `metadata`. Non-cache throttling and migration state belongs under the external data root's `state/`. Run `migrate-metadata-cache` after `doctor`; it canonicalizes complete caches and removes obsolete or duplicate artifacts.

Never write runtime artifacts into TASTE, the current workspace, or another Git repository unless the user explicitly requests an export. The backend rejects any Git-contained write by default.

## Defaults

Apply explicit user settings first, then `references/defaults.json`:

- Metadata shortlist for full-text acquisition and per-paper deep reading: 100.
- Final recommendations: 20.
- When the user specifies neither time nor channels, the mandatory default scope is all three major conferences—NeurIPS/NIPS, ICLR, and ICML—at each venue's latest single year with usable complete metadata, plus arXiv over the inclusive trailing six calendar months.
- Under that default scope, independently add exactly one to three highest-value topic-specific channels and record why. Extra conferences use their latest usable complete-metadata year; extra preprint/journal streams use the same concrete six-month dates as arXiv.
- Conferences: exactly the latest single year with usable metadata; resolve it with `probe-venue`. Every included conference source must set `complete_catalog: true` and crawl the entire official venue catalog. Never put `limit`, `max_results`, or sampling parameters on a conference source.
- arXiv: inclusive trailing six calendar months using concrete ISO dates. Unless the user explicitly supplies categories, exhaustively acquire `cs.AI`, `cs.LG`, `stat.ML`, `cs.CL`, `cs.CV`, `cs.IR`, `cs.RO`, `eess.SY`, `cs.MA`, and `cs.NE`. This deliberately covers core AI plus NLP (`cs.CL`), computer vision (`cs.CV`), retrieval/RAG (`cs.IR`), and embodied intelligence through robotics and systems/control (`cs.RO` + `eess.SY`). Never apply task keywords or a result limit during this base crawl. Cache complete daily shards under `metadata/arxiv/<category>/<YYYY-MM-DD>.json`; task-specific title/abstract relevance is decided later by Codex.
- An arXiv day is reusable only after the local calendar day has closed (`shard_date < today`). The current day and any future day must be written `complete: false`, `provisional: true` and refetched on every later crawl; API pagination exhaustion never makes an open calendar day complete. Page checkpoints live under the external data root at `state/arxiv-staging/`, survive failed requests, and are deleted immediately after their month/range is successfully published into daily shards. Never create `metadata/arxiv/.staging`.
- bioRxiv: inclusive trailing six calendar months using concrete ISO dates. Exhaustively cache every category without task-keyword filtering or result limits as `metadata/biorxiv/<YYYY-MM-DD>.json`. The current day is always provisional and refetched; the preceding three closed days are reconciled at most every 24 hours; older proven-complete days are reused indefinitely unless explicitly refreshed. Cursor checkpoints live only under `state/biorxiv-staging/`, survive failed ranges, and are deleted after successful daily-shard publication.
- Download and deep-read every shortlisted paper with available full text. Produce one validated Chinese `papers/<index>/read.md` per paper, then aggregate all of them into the run-level `read.md`.
- Cache: reuse entries no older than seven days by default; user refresh overrides this.

Read `references/migration-parity.md` and `references/service-contract.md` before commands, and `references/scoring-rubric.md` before scoring. Never weaken a locked migrated invariant for convenience.

## Required workflow

1. Run `doctor`, then `migrate-metadata-cache`, then `init-run` once. Require the migration inventory to report `unified: true`. Keep the returned `run_dir` for the entire request.
2. Interpret the request. Infer goals, evidence needs, useful adjacent fields, exclusions, and priority among fit, rigor, novelty, reproducibility, recency, and transferability.
   Always write `request_scope` into the plan with boolean `user_specified_time`, boolean `user_specified_channels`, and concrete `as_of_date`. Do not treat a topic, paper count, or phrase such as “调研论文” as an implicit time/channel choice. If both booleans are false, the backend-enforced default is the three major conferences plus six-month arXiv and one to three topic-adaptive channels; do not ask the user to restate these defaults.
3. Use `catalog` and `probe-venue` to resolve channels. Start from the four baseline channels, then choose additional high-value channels from the topic. Write `channel_decisions` for every considered channel. Preserve the original Find semantics: once a venue/year is resolved, its complete metadata/title pool is acquired and cached first. Never put queries, categories, tracks, or limits on a venue source. Topic and category reasoning happens only after the complete pool exists, during Codex scoring/shortlisting.
4. Write the plan only to `<run_dir>/plan.json`. Run `metadata --run-dir ... --plan ...`. Inspect every source receipt. Repair failed or weak coverage before scoring; never interpret source failure as absence of literature.
   Metadata crawls can run for many minutes. If command execution returns a live session/cell ID, it is still running: keep waiting/polling that exact session until its exit code is available. Never describe a yielded session as interrupted, never start a replacement metadata command for the same run, and never continue to scoring without the command's final JSON receipt. For bioRxiv require `status: complete`, `exhausted: true`, `truncated: false`, `closed_days_complete: true`, the expected daily-shard count, and an `exhaustion_proof` beginning with `all_closed_days_complete`. If the interval contains today, require it in `provisional_days`; never add a limit or treat API exhaustion as proof that the open day has closed.
   HTTP 429/503 responses are transient backend events: the service must honor `Retry-After`, wait, and retry internally. Do not announce a source problem while retries or a live metadata process remain. Report a source failure only after the command exits and its receipt proves that all backend retry attempts were exhausted.
   For arXiv require `exhausted: true`, `truncated: false`, `closed_days_complete: true`, the expected category×day shard count, and an `exhaustion_proof` beginning with `all_closed_category_days_complete`. If the requested interval includes the local current day, require that date in `provisional_days` and never accept it as a reusable complete shard. A task query may influence later Codex scoring but must not narrow these cached base shards.
5. Score every paper in `<run_dir>/metadata.json` yourself using the metadata rubric. The scientific judgment at this stage must use only each paper's title and abstract; do not inspect full text yet, and do not let venue prestige, publication date, identifiers, code links, or PDF availability add ranking points. Write raw scores outside the workspace, preferably `<run_dir>/metadata_scores.raw.json`. Run `shortlist --scores ... --target N`, using 100 unless the user explicitly requests another count. The backend rejects missing, duplicate, foreign, or arithmetically invalid scores and creates canonical `metadata_scores.json` and `shortlist.json`. Only papers in this shortlist proceed to full-text acquisition or deep reading.
6. Run `fulltext --run-dir ... --workers ...`. For OpenReview papers the backend must first use the official `openreview-py` attachment API, authenticated when credentials exist and anonymous otherwise; a public `/pdf?id=` URL is only a fallback. The backend then validates identity/body content, caches successful full text by stable identity, and resumes by identity.
7. Run `prepare-reads --run-dir ...`. It restores single-paper reads only under the current reading-contract version and when the exact full-text SHA-256 is unchanged; otherwise it writes the paper to `reading_queue.json`. For each pending paper, start a dedicated Codex subagent with only that queue item. Maximize concurrency: keep every subagent slot that the current Codex environment makes available filled, dispatch one paper per subagent, and immediately refill a slot when its paper finishes until the queue is empty. The main Codex remains the coordinator and validator; never assign multiple papers to one subagent merely to reduce agent count. The subagent must inspect the entire exact `full_text_path`, write only the exact `read_path` and `receipt_path`, and return a verifiable `dedicated_codex_subagent` audit. Wait for every dispatched subagent and validate its distinct artifact. Never batch-generate prose, reuse a prose template, or generate a read from metadata/abstract alone. If dedicated subagents are unavailable, stop and report that deep reading is blocked; do not silently replace this stage with batch generation or claim completion.
8. Every single-paper `read.md` must be Chinese and use exactly: the paper title; source and URL/PDF lines; `摘要`; `动机与核心创新` with `动机：` and `核心创新：`; `方法` with a paper-specific relevant LaTeX formula and plain-language explanation; `实验结果`; and `优缺点总结`. When `abstract_zh` is absent, `摘要` must be the complete Chinese translation of the supplied original English abstract—not a summary—while preserving model names, dataset names, numbers, conclusions, and limitations. The strengthened receipt maps every source sentence to its translation so completeness can be checked mechanically. Follow the original Reading length and Markdown rules in the contract.
9. Run `validate-reads --run-dir ...`. It applies the migrated Reading gates: dedicated-subagent completion receipt, exact article/full-text paths, whole-text `evidence_chars`, Chinese abstract quality, fixed Chinese abstract preservation, title/section/length/formula checks, and full-text revision-bound cache validity. Repair each failed paper independently and rerun until `status: complete`. Only then may the backend publish current-version single-paper reads and aggregate `<run_dir>/read.md`.
10. Persist one JSONL evidence card per paper. Cite both exact absolute `full_text_path` and `read_path`. Run `finalize --evidence-cards ... --target N`; it rejects papers without validated reads, incomplete evidence, invalid arithmetic, or mismatched paths.
11. Re-read close calls around the cutoff and correct artifacts when necessary.
12. Write `<run_dir>/recommendations.md` as the final synthesis: cross-paper themes, agreements/conflicts, reusable methods, evidence gaps, and each final recommendation. Run `complete --recommendations ...`.
13. Report the synthesis to the user, including coverage, full-text gaps, validated single-paper-read count, read/scored count, and recommendation shortfall.

## Evidence rules

- Metadata relevance is triage, not scientific quality.
- Preserve paper identity across every fallback; never substitute a similar paper.
- Do not infer full-text results from abstracts.
- Separate reported evidence from Codex inference.
- Record retraction/supersession, identity uncertainty, unsupported claims, or critical missing evidence explicitly in evidence and confidence; do not silently invent extra ranking dimensions beyond the original match/transferability scores.
- Never claim completion while `run.json` is not `status: complete`.
- Never upload or push unless separately requested.

## Command sequence

```text
<python-prefix> <service> doctor
<python-prefix> <service> migrate-metadata-cache
<python-prefix> <service> init-run
<python-prefix> <service> catalog --query ICLR
<python-prefix> <service> probe-venue --venue-id iclr
<python-prefix> <service> metadata --run-dir <run-dir> --plan <run-dir>/plan.json
<python-prefix> <service> shortlist --run-dir <run-dir> --scores <run-dir>/metadata_scores.raw.json --target 100
<python-prefix> <service> fulltext --run-dir <run-dir> --workers 8
<python-prefix> <service> prepare-reads --run-dir <run-dir>
<python-prefix> <service> validate-reads --run-dir <run-dir>
<python-prefix> <service> finalize --run-dir <run-dir> --evidence-cards <run-dir>/evidence_cards.jsonl --target 20
<python-prefix> <service> complete --run-dir <run-dir> --recommendations <run-dir>/recommendations.md
```
