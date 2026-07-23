# Channel ownership

These are production contracts, not availability checks. Each metadata route
must exhaust the requested edition/date range, provide real abstracts, pass its
channel validator, and only then publish a cache. Full text always passes the
shared body and paper-identity validation before publication.

| Channel | Complete metadata and abstract route | First official full-text route | Cache namespace |
|---|---|---|---|
| NeurIPS/NIPS | NeurIPS proceedings index, then every official abstract page using TASTE's marker parser | Deterministic NeurIPS paper PDF | `metadata/neurips/<year>.json` |
| ICLR | Exhaustive OpenReview `venueid` pagination through the shared OpenReview gate | OpenReview attachment/direct PDF | `metadata/iclr/<year>.json` |
| ICML | ICML virtual paper index, every paper detail page; PMLR/OpenReview identifiers remain full-text fallbacks | ICML/PMLR/OpenReview official PDF | `metadata/icml/<year>.json` |
| KDD | Official/DBLP complete title pool plus exact-identity TASTE-style abstract enrichment and resumable checkpoints | ACM DOI PDF | `metadata/kdd/<year>.json` |
| SIGIR | Official proceedings/program pool plus exact-identity enrichment and resumable checkpoints | ACM DOI PDF | `metadata/sigir/<year>.json` |
| CIKM | Official proceedings when complete, otherwise complete title pool plus exact-identity enrichment | ACM DOI PDF | `metadata/cikm/<year>.json` |
| AAAI | All matching official AAAI OJS issues and every article detail page | OJS article PDF | `metadata/aaai/<year>.json` |
| ICCV | CVF Open Access index and every paper detail page | CVF paper PDF | `metadata/iccv/<year>.json` |
| WWW | Official schedule/accepted pool plus DBLP merge and exact-identity enrichment | ACM DOI PDF | `metadata/www/<year>.json` |
| CVPR | CVF Open Access index and every paper detail page | CVF paper PDF | `metadata/cvpr/<year>.json` |
| ACL | Complete ACL Anthology yearly XML; missing XML/detail abstracts use the official PDF | ACL Anthology PDF | `metadata/acl/<year>.json` |
| IJCAI | Official proceedings index plus resumable official-PDF abstract extraction; accepted-paper page is the pre-proceedings route | Deterministic IJCAI PDF | `metadata/ijcai/<year>.json` |
| ECCV | ECVA virtual index and every paper detail page | Deterministic ECVA archive PDF | `metadata/eccv/<year>.json` |
| EMNLP | Complete matching ACL Anthology XML volumes; missing XML/detail abstracts use the official PDF | ACL Anthology PDF | `metadata/emnlp/<year>.json` |
| arXiv | Exhaustive category/date Atom pagination partitioned into closed-day shards | arXiv official PDF | `metadata/arxiv/<category>/<day>.json` |
| bioRxiv | Exhaustive cursor pagination partitioned into daily shards with version reconciliation | bioRxiv DOI/version PDF | `metadata/biorxiv/<day>.json` |

Conference caches are accepted only when payload/source/cache-key,
channel/schema/year, receipt counts, unique identities, row venues and any
minimum catalog size all match; catalog exhaustion and a real abstract for
every record remain mandatory. arXiv and bioRxiv shards bind their source,
date/category, identifiers and full abstracts, and distinguish immutable
closed days from provisional current-day shards. Full-text caches are
channel-qualified, hashed and acquisition-contract-versioned under
`fulltext/<channel>/<identity-hash>/`.

Source concurrency is channel-owned. Independent channels may run together;
OpenReview shares one process-safe slot by default with a hard ceiling of three,
ACM-family metadata is challenge-aware and serial at the service gate, and
arXiv uses one spaced slot. `Retry-After`, cross-process cooldowns, bounded
optional fallbacks, and resumable production staging/checkpoints apply to
requests themselves. These checkpoints are runtime state, never an alternate
availability-only or sampled acquisition path.
