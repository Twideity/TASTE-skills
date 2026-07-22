# Multi-turn research modes

Treat this skill as a set of composable research stages, not a mandatory one-shot pipeline. Choose the smallest workflow that can answer the current turn with real evidence.

## Setting precedence

Apply settings in this order:

1. The user's explicit instructions in the current turn.
2. Constraints already established in the current conversation, unless the user changed them.
3. Reusable evidence and scope from the parent run.
4. `defaults.json` only for unspecified fields.

Never reinterpret a default as a minimum. Counts, venues, dates, arXiv categories, cache policy, reading backend, stopping stage, scoring priorities, output language, and final format are independently customizable each turn. Selecting `codex_fast` must record either `user_disabled_claude=true` or `claude_unavailable=true`; speed preference alone cannot silently bypass the default Claude contract.

Treat a user-originated reading-backend choice as conversation state, not a one-run option. Unless the user explicitly says the choice applies only to the current turn, normalize a no-Claude instruction to `reading_preference=codex_fast`, `user_disabled_claude=true`, `reading_preference_scope=conversation`, and `conversation_reading_preference_locked=true`. Carry it across follow-ups, topic changes, parent/child runs, and fresh plans in the same Codex conversation. Silence is not revocation. Clear or replace it only on an explicit user instruction such as “后续重新使用 Claude 精读”. Claude unavailability is operational state rather than a user preference and need not remain locked after availability changes.

## Modes

- `comprehensive`: End-to-end literature review. When the user supplied neither time nor channels, apply the full default NeurIPS/ICLR/ICML + six-month arXiv + adaptive-channel scope and the default 100/20 counts.
- `focused`: A bounded question or narrow direction. Codex chooses only the useful channels, dates, shortlist size, stopping stage, and recommendation count, records its rationale, and may answer after any sufficient stage.
- `incremental`: Extend or challenge a previous result. Create a child run with `init-run --parent-run-dir ...`; read the parent artifacts first, search only the evidence gap, and reuse global metadata/full-text/read caches.
- `metadata_only`: Discover terminology, authors, venues, candidate papers, or trends without downloading/reading every full text. Stop after metadata or shortlist unless the evidence need changes.

Use the existing run without new crawling when its validated evidence already answers the follow-up. Create a child run when the research question, corpus, dates, or ranking objective materially changes. Never overwrite a completed parent run merely to explore another direction.

## Stage composition

Valid stopping points are `metadata`, `shortlist`, `fulltext`, `reading`, and `recommendation`.

- Use metadata only for landscape/orientation questions and candidate lists; label conclusions as metadata-level evidence.
- Acquire selected full texts when the user asks about methods, experiments, limitations, comparisons, or factual claims not supported by abstracts.
- Reuse existing validated reads when the same paper reappears under a new question, then rescore it for the new objective rather than rereading by default.
- Run final recommendation machinery only when the user asks for ranking, selection, comparison, or synthesis.

## Multi-turn evidence discipline

Keep each run's `research_question`, `workflow`, source decisions, targets, reading preference, and stopping stage explicit. A follow-up may change any of them without changing unrelated settings. Cite exact parent/current artifact paths internally, preserve uncertainty, and distinguish metadata evidence, full-text reported evidence, and Codex inference. Do not claim that Codex permanently learned a paper; knowledge remains grounded in reusable run/cache artifacts and must be reread when needed.
