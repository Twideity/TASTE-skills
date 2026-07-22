# Ranking rubric

## Title-and-abstract triage (0–100)

- `topic_fit` — direct topical and task fit visible in the title and abstract: 0–50.
- `transferability_potential` — method or evidence likely transferable to the user's goal, supported by the title and abstract: 0–30.
- `abstract_specificity` — how concretely the abstract states its method, evidence, results, or limitations: 0–20.

Record `metadata_score`, component scores, a one-sentence inclusion reason, and uncertainty. Use only title and abstract as scientific evidence in this phase. Venue/source prestige, recency, identifiers, code/data links, and full-text availability receive no points; retain them only for identity, coverage reporting, and deterministic tie inspection.

Score every metadata paper, including obvious exclusions. Use low scores and explicit reasons rather than omitting rows; the backend rejects incomplete coverage.

Sort all scored metadata and send only the top 100 to full-text acquisition and per-paper deep reading unless the user explicitly overrides this count.

## Full-text ranking (0–20)

Preserve the original Reading unified-scoring semantics:

- `match_score` (0–10): similarity and direct relevance to the research topic, interests, constraints, and researcher profile.
- `transferability_score` (0–10): practical usefulness to the research, especially whether methods, mechanisms, experimental designs, or evaluation protocols can be borrowed.

`final_score` is exactly their sum. For synthesis, also record confidence, decisive full-text evidence, limitations, and `borrowable_elements`; these evidence fields explain and calibrate the scores but do not introduce new ranking dimensions.

Re-read close calls at the final cutoff and resolve equal totals by the user's stated priority between match and transferability, not arbitrary decimal precision.

Create an evidence card only after opening the exact acquired `text_path` and completing its validated single-paper `read.md`. Include exact absolute `full_text_path` and `read_path`. The backend requires one card for every successful full-text acquisition.
