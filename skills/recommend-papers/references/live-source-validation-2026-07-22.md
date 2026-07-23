# Live conference-source validation — 2026-07-22

This is an observed network snapshot, not a release-calendar prediction and not
a permanent availability table.  `probe-venue` must repeat these checks when the
skill is used.  A missing proceedings directory never suppresses another live
adapter for the same year.

| Venue | 2026 channel observed | Result at validation time |
|---|---|---|
| NeurIPS/NIPS | official proceedings; authenticated OpenReview group | proceedings returned 404; OpenReview returned zero public venue notes |
| ICLR | authenticated OpenReview | 5,351 records; every record had abstract and OpenReview PDF locator |
| ICML | authenticated OpenReview; official virtual site | 6,341 OpenReview records with abstracts/PDF locators; virtual index exposed 6,629 detail links |
| SIGKDD/KDD | DBLP year index and split XML volumes | 256 title/DOI seeds; indexed abstract/public-copy enrichment remains required |
| SIGIR | DBLP year index/XML | 685 title/DOI seeds; indexed abstract/public-copy enrichment remains required |
| CIKM | DBLP year index/XML; official program route | no usable 2026 corpus was exposed |
| WWW | DBLP main and companion XML | 1,191 title/DOI seeds across discovered volumes |
| AAAI | official OJS archive | 48 2026 issues were discoverable; sampled articles exposed author, abstract, and official download URL |
| ICCV | CVF Open Access | 2026 is not a regular ICCV year; CVF returned 404 and the parity rule resolves the latest odd year |
| CVPR | CVF Open Access | 4,068 paper detail links; sampled detail page returned HTTP 200 |
| ACL | official ACL Anthology XML | 2,650 records; 2,650 abstracts and 2,650 deterministic PDF URLs |
| IJCAI | proceedings directory; official accepted-papers page | proceedings returned 404, but accepted-papers yielded 982 valid titled records with 982 authors and 982 abstracts |
| ECCV | ECVA virtual index; authenticated OpenReview group | virtual index returned HTTP 200 with zero papers; OpenReview returned zero public venue notes |
| EMNLP | ACL Anthology XML; conference accepted-paper page | XML returned 404; the apparent 2026 page contained 2025 metadata/content and was rejected as stale |

The IJCAI pre-proceedings path was also tested end to end.  One journal-track
entry omitted its abstract on the IJCAI page; exact-title arXiv enrichment found
the same paper, and full-text acquisition downloaded and identity/body-validated
128,930 extracted characters.  The accepted-page corpus then passed at 982/982
abstract coverage.  Eight `Title TBD` schedule placeholders were intentionally
excluded because they are not paper metadata.

These results mean that ICLR, ICML, KDD, SIGIR, WWW, AAAI, CVPR, ACL, and IJCAI
already exposed useful 2026 data on the validation date even though not every
archival proceedings site was final.  They do not justify inventing records for
channels that actually returned zero, 404, or stale prior-year content.

Follow-up on 2026-07-23: an isolated run-aware KDD probe completed in roughly
30 seconds. It discovered the same 256-title DBLP pool, selected exactly three
rows, and performed enrichment only for those three. One sample obtained an
abstract; two remained sample-partial after ACM 403 and Semantic Scholar 429
events. The year was still correctly reported as available, while the receipt
kept `complete_catalog: false` and required a later formal full crawl.

Later live validation on 2026-07-23 superseded the incomplete ACM-family
assumptions for two venues. CIKM 2025's conference-hosted proceedings page
exposed 852 DOI-linked rows with authors and embedded abstracts; the installed
formal metadata command completed at 852/852 with no warning or coverage
notice. SIGIR 2026's official 104-page program PDF exposed a current 667-paper
program: 665 abstracts were embedded in the PDF, and the two genuine omissions
were exact-title/DOI matched to DBLP and filled by one bounded Semantic Scholar
batch call. The installed formal command completed at 667/667 with no active
warning. These official pages must be tried before treating either venue as a
large generic ACM enrichment job.

WWW 2026 remains different. Its official full schedule exposed 1,159 unique
paper rows, including authors and 1,152 exact ACM DOIs, but ordinary paper
nodes did not embed abstracts. The main and companion ACM DL links, citation
export endpoints, abstract pages, and PDF/EPDF endpoints all returned the same
HTTP 403 automation challenge to a normal backend client. Crossref returned
the tested title/authors but no abstract. The research-track submission system
was EasyChair, not a public OpenReview corpus. Exact DOI indexes and verified
OA copies therefore remain necessary unless an authorized ACM TDM API is
configured. OpenAlex changed in 2026 to require a free API key for normal
at-scale use; anonymous calls can exhaust a shared daily allowance and must not
be expanded into hours of serial backoff.

WWW follow-up validation on 2026-07-23 quantified the indexed fallbacks against
all 1,191 DOI-bearing rows. The existing cache had 213 abstracts. Semantic
Scholar's DOI batch returned 1,188 exact identities and raised the union to
1,071 abstracts. OpenAIRE's legacy multi-DOI search added two exact abstracts;
its v3 exact-title API found all 118 then-unresolved publication identities and
added two more abstracts, including FovRL and FediScan from Aaltodoc. Their
repository PDFs extracted 58,331 and 65,405 characters and passed title/author
identity checks. A same-title Zenodo dataset carrying the paper DOI was tested
and rejected: its dataset description is not the paper abstract. The verified
union is therefore 1,075/1,191 abstracts, with 116 still awaiting a real paper
abstract or identity-validated full text. HAL returned
zero records for a 50-DOI sample, and ten evenly
spaced unresolved DOI lookups all returned Crossref 404. Thus the remaining
gap is a late-indexing cohort, not evidence that the papers do not exist.
OpenAIRE DOI and exact-title routes must both run, repository locations must be
cached for later full-text acquisition, and a keyed OpenAlex quota must use a
credential-scoped cooldown rather than inherit an exhausted anonymous state.

A formal refresh after the channel audit merged 1,212 official-schedule/DBLP
candidates, checkpointed 1,071 verified abstracts, and correctly refused to
publish the incomplete corpus with 141 remaining. This run exhausted the
anonymous OpenAIRE 60-request hourly window accumulated during validation while
the anonymous OpenAlex daily state was already exhausted. Both waits must be
reported; resume the same source after reset instead of silently retrying every
paper or treating the partial checkpoint as a complete metadata cache.
