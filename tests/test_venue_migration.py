from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace


SERVICE_ROOT = Path(__file__).resolve().parents[1] / "skills" / "recommend-papers" / "scripts"
sys.path.insert(0, str(SERVICE_ROOT))

from recommend_service.conference_sources import official_pdf_candidates  # noqa: E402
from recommend_service.fulltext import _authenticated_openreview_pdf, _candidate_urls, _identity_ok  # noqa: E402
import recommend_service.fulltext as fulltext_module  # noqa: E402
import recommend_service.claude_reads as claude_reads_module  # noqa: E402
import recommend_service.cli as cli_module  # noqa: E402
import recommend_service.http as http_module  # noqa: E402
from recommend_service.http import _slot_path, concurrency_limit, request_slot, service_call, service_for  # noqa: E402
from recommend_service.metadata import DEFAULT_VENUES, PRIORITY_VENUE_NAMES, _venue_year_candidates, fetch_venue, validate_plan, venue_metadata_audit  # noqa: E402
from recommend_service.pipeline import finalize, finish_stage  # noqa: E402
from recommend_service.reading_artifacts import prepare_fast_batches, prepare_reads  # noqa: E402
from recommend_service.storage import stable_hash  # noqa: E402
from recommend_service.venue_sources import _authors_from_soup, _dblp_xml_urls_from_index, _parse_ijcai_accepted  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402


class VenueMigrationTests(unittest.TestCase):
    def test_all_priority_venues_are_catalogued(self) -> None:
        ids = {row["id"] for row in DEFAULT_VENUES}
        self.assertTrue({"neurips", "iclr", "icml", "kdd", "sigir", "cikm", "www", "aaai", "iccv", "cvpr", "acl", "ijcai", "eccv", "emnlp"}.issubset(ids))

    def test_priority_cache_requires_abstracts(self) -> None:
        row = {"title": "A Real Research Paper", "authors": ["A. Author"], "url": "https://example.test/p", "abstract": ""}
        self.assertFalse(venue_metadata_audit([row], require_complete_abstracts=True)["metadata_completeness_ok"])

    def test_original_official_pdf_derivations(self) -> None:
        cases = [
            ({"venue": "NeurIPS", "url": "https://proceedings.neurips.cc/paper_files/paper/2025/hash/abc-Abstract-Conference.html"}, "proceedings.neurips.cc/paper_files/paper/2025/file/abc-Paper-Conference.pdf"),
            ({"venue": "ICLR", "url": "https://proceedings.iclr.cc/paper_files/paper/2026/hash/abc-Abstract-Conference.html"}, "proceedings.iclr.cc/paper_files/paper/2026/file/abc-Paper-Conference.pdf"),
            ({"venue": "ICML", "url": "https://proceedings.mlr.press/v300/example.html"}, "proceedings.mlr.press/v300/example/example.pdf"),
            ({"venue": "CVPR", "url": "https://openaccess.thecvf.com/content/CVPR2025/html/Test_Paper.html"}, "openaccess.thecvf.com/content/CVPR2025/papers/Test_Paper.pdf"),
            ({"venue": "ICCV", "url": "https://openaccess.thecvf.com/content/ICCV2025/html/Test_Paper.html"}, "openaccess.thecvf.com/content/ICCV2025/papers/Test_Paper.pdf"),
            ({"venue": "ACL", "url": "https://aclanthology.org/2025.acl-long.1/"}, "aclanthology.org/2025.acl-long.1.pdf"),
            ({"venue": "EMNLP", "url": "https://aclanthology.org/2025.emnlp-main.1/"}, "aclanthology.org/2025.emnlp-main.1.pdf"),
            ({"venue": "IJCAI", "url": "https://www.ijcai.org/proceedings/2025/1"}, "ijcai.org/proceedings/2025/0001.pdf"),
            ({"venue": "KDD", "identifiers": {"doi": "10.1145/123.456"}}, "dl.acm.org/doi/pdf/10.1145/123.456"),
            ({"venue": "SIGIR", "identifiers": {"doi": "10.1145/123.457"}}, "dl.acm.org/doi/pdf/10.1145/123.457"),
            ({"venue": "CIKM", "identifiers": {"doi": "10.1145/123.458"}}, "dl.acm.org/doi/pdf/10.1145/123.458"),
            ({"venue": "WWW", "identifiers": {"doi": "10.1145/123.459"}}, "dl.acm.org/doi/pdf/10.1145/123.459"),
            ({"venue": "AAAI", "url": "https://ojs.aaai.org/index.php/AAAI/article/view/123/456"}, "ojs.aaai.org/index.php/AAAI/article/view/123/456"),
            ({"venue": "AAAI", "pdf_url": "https://ojs.aaai.org/index.php/AAAI/article/download/37413/41375"}, "ojs.aaai.org/index.php/AAAI/article/download/37413/41375"),
        ]
        for paper, expected in cases:
            urls = [item["url"] for item in official_pdf_candidates(paper)]
            self.assertTrue(any(expected in url for url in urls), (paper, urls))

    def test_arbitrary_metadata_pdf_is_not_mislabeled_official(self) -> None:
        self.assertEqual(official_pdf_candidates({"venue": "ACL", "pdf_url": "https://mirror.example/paper.pdf"}), [])

    def test_conference_hosts_have_independent_cooldowns(self) -> None:
        self.assertEqual(service_for("https://aclanthology.org/2025.acl-long.1.pdf"), "host-aclanthology.org")
        self.assertEqual(service_for("https://openaccess.thecvf.com/content/CVPR2025/papers/a.pdf"), "host-openaccess.thecvf.com")
        self.assertNotEqual(service_for("https://aclanthology.org/x"), service_for("https://openaccess.thecvf.com/x"))
        self.assertNotEqual(_slot_path("host-aclanthology.org", 0), _slot_path("host-openaccess.thecvf.com", 0))

    def test_http_concurrency_is_channel_specific_and_overridable(self) -> None:
        self.assertEqual(concurrency_limit("arxiv"), 1)
        self.assertEqual(concurrency_limit("openreview"), 1)
        self.assertEqual(concurrency_limit("acm"), 1)
        self.assertGreater(concurrency_limit("host-aclanthology.org"), 1)
        with patch.dict(os.environ, {"RECOMMEND_PAPERS_HTTP_CONCURRENCY": '{"arxiv": 2, "default_host": 5}'}, clear=False):
            self.assertEqual(concurrency_limit("arxiv"), 2)
            self.assertEqual(concurrency_limit("host-new-conference.example"), 5)
        with patch.dict(os.environ, {"RECOMMEND_PAPERS_HTTP_CONCURRENCY": '{"openreview": 100}'}, clear=False):
            self.assertEqual(concurrency_limit("openreview"), 3)

    def test_openreview_client_rate_limit_enters_shared_cooldown_and_retries(self) -> None:
        calls = 0

        def client_call():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("429 RateLimitError: try again in 0 seconds")
            return "ok"

        with TemporaryDirectory() as temporary, \
                patch.object(http_module, "HTTP_STATE_ROOT", Path(temporary)), \
                patch.dict(http_module.MIN_INTERVALS, {"generic": 0}, clear=True):
            value, history = service_call("openreview", client_call, max_attempts=3)
        self.assertEqual(value, "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(history[0]["status_code"], 429)
        self.assertEqual(history[0]["retry_after_seconds"], 0)

    def test_different_channels_and_configured_same_channel_slots_overlap(self) -> None:
        def assert_overlap(services: list[str], limits: dict[str, int]) -> None:
            barrier = threading.Barrier(len(services))

            def enter(service: str) -> bool:
                with request_slot(service):
                    barrier.wait(timeout=2)
                    return True

            with TemporaryDirectory() as temporary, \
                    patch.object(http_module, "HTTP_STATE_ROOT", Path(temporary)), \
                    patch.dict(http_module.MIN_INTERVALS, {"generic": 0}, clear=True), \
                    patch.dict(os.environ, {"RECOMMEND_PAPERS_HTTP_CONCURRENCY": json.dumps(limits)}, clear=False), \
                    ThreadPoolExecutor(max_workers=len(services)) as pool:
                self.assertTrue(all(pool.map(enter, services)))

        assert_overlap(["host-channel-a.example", "host-channel-b.example"], {
            "host-channel-a.example": 1,
            "host-channel-b.example": 1,
        })
        assert_overlap(["host-same-channel.example", "host-same-channel.example"], {
            "host-same-channel.example": 2,
        })

    def test_optional_oa_lookup_cooldown_never_blocks_official_candidate(self) -> None:
        paper = {
            "venue": "ACL",
            "pdf_url": "https://aclanthology.org/2025.acl-long.1.pdf",
            "identifiers": {"doi": "10.18653/v1/2025.acl-long.1"},
        }
        with patch("recommend_service.fulltext.cooldown_remaining", return_value=3600), patch("recommend_service.fulltext.get") as mocked_get:
            candidates, receipts = _candidate_urls(paper)
        self.assertFalse(mocked_get.called)
        self.assertEqual(candidates[0]["kind"], "conference_official_pdf")
        self.assertTrue(any(item.get("kind") == "openalex_lookup" and item.get("status") == "skipped_cooldown" for item in receipts))

    def test_current_year_fulltext_can_use_exact_title_public_copy(self) -> None:
        response = SimpleNamespace(
            ok=True,
            url="https://api.semanticscholar.org/graph/v1/paper/search",
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b"{}",
            json=lambda: {"data": [{
                "title": "A Current-Year Accepted Paper",
                "openAccessPdf": {"url": "https://authors.example/current-year.pdf"},
                "externalIds": {},
            }]},
        )

        def cooldown(service: str) -> float:
            return 0 if service == "semantic_scholar" else 3600

        with patch("recommend_service.fulltext.cooldown_remaining", side_effect=cooldown), patch("recommend_service.fulltext.get", return_value=response):
            candidates, _ = _candidate_urls({"title": "A Current-Year Accepted Paper", "venue": "IJCAI"})
        self.assertIn(
            {"url": "https://authors.example/current-year.pdf", "kind": "semantic_scholar_exact_title_pdf"},
            candidates,
        )

    def test_dblp_multivolume_year_is_discovered_to_exhaustion(self) -> None:
        html = """
        <a href="kdd2025-1.html">KDD 2025 volume 1</a>
        <a href="kdd2025-2.html">KDD 2025 volume 2</a>
        <a href="kdd2025-2.html#toc">duplicate</a>
        <a href="kdd2024.html">older year</a>
        <a href="https://dblp.org/rec/conf/kdd/2025-1.html">record</a>
        """
        self.assertEqual(
            _dblp_xml_urls_from_index(html, "https://dblp.org/db/conf/kdd/index.html", "kdd", 2025),
            [
                "https://dblp.org/db/conf/kdd/kdd2025-1.xml",
                "https://dblp.org/db/conf/kdd/kdd2025-2.xml",
            ],
        )

    def test_virtual_conference_jsonld_authors_are_preserved(self) -> None:
        soup = BeautifulSoup(
            '<script type="application/ld+json">{"@type":"CreativeWork","author":[{"@type":"Person","name":"Ada Lovelace"},{"@type":"Person","name":"Alan Turing"}]}</script>',
            "html.parser",
        )
        self.assertEqual(_authors_from_soup(soup), ["Ada Lovelace", "Alan Turing"])

    def test_ijcai_current_year_accepted_page_is_metadata_source(self) -> None:
        html = """
        <li class="ij-paper">
          <span class="ij-pid">#1234</span>
          <div class="ij-ptitle">A Real Current-Year Research Paper</div>
          <span class="ij-author">Ada Lovelace</span>
          <span class="ij-author">Alan Turing</span>
          <div class="ij-abstract">This abstract is long enough to describe the actual method, experiments, and conclusions of the accepted paper in detail.</div>
          <span class="ij-kw">Machine Learning</span>
        </li>
        """
        rows = _parse_ijcai_accepted(html, year=2026, page_url="https://2026.ijcai.org/accepted-papers/")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["identifiers"]["ijcai_paper_id"], "1234")
        self.assertEqual(rows[0]["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertIn("actual method", rows[0]["abstract"])
        self.assertEqual(rows[0]["year"], 2026)

    def test_single_paper_queue_item_contains_every_required_input(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            text_path = run_dir / "full_text.txt"
            text_path.write_text("Abstract\nIntroduction\nMethod\nResults\nReferences\n" + "evidence " * 300, encoding="utf-8")
            payload = {
                "items": [{
                    "index": 1,
                    "identity": "doi:10.1234/queue-test",
                    "full_text_available": True,
                    "text_path": str(text_path),
                    "pdf_path": str(run_dir / "paper.pdf"),
                    "paper": {
                        "title": "A Self Contained Queue Paper",
                        "abstract": "A complete source abstract for translation.",
                        "authors": ["Ada Lovelace"],
                        "venue": "ICLR",
                        "published": "2026-01-01",
                        "url": "https://openreview.net/forum?id=queue",
                        "pdf_url": "https://openreview.net/pdf?id=queue",
                        "identifiers": {"doi": "10.1234/queue-test"},
                    },
                    "attempts": {"pdf": [{"accepted": True, "url": "https://openreview.net/pdf?id=queue"}]},
                }],
            }
            (run_dir / "full_text_results.json").write_text(json.dumps(payload), encoding="utf-8")
            queue = prepare_reads(run_dir)
            item = queue["pending"][0]
            for key in ("paper_url", "pdf_url", "local_pdf_path", "run_dir", "full_text_path", "read_path", "receipt_path", "prompt_path", "source_abstract", "full_text_sha256"):
                self.assertTrue(item.get(key), key)
            prompt_text = Path(item["prompt_path"]).read_text(encoding="utf-8")
            self.assertIn("从头到尾分块检查全文", prompt_text)
            self.assertIn(item["read_path"], prompt_text)
            self.assertIn(item["receipt_path"], prompt_text)

    def test_pdf_identity_uses_front_matter_title_and_author(self) -> None:
        paper = {"title": "Graph Neural Networks for Reliable Cats", "authors": ["Ada Lovelace", "Alan Turing"]}
        matching = "Graph Neural Networks for Reliable Cats\nAda Lovelace, Alan Turing\nAbstract\n" + "body " * 300
        wrong = "Unrelated Vision Transformers\nGrace Hopper\nAbstract\nGraph Neural Networks for Reliable Cats is cited later."
        self.assertTrue(_identity_ok(paper, matching))
        self.assertFalse(_identity_ok(paper, wrong))

    def test_openreview_uses_original_submission_attachment_fallback(self) -> None:
        class FakeClient:
            def get_attachment(self, *, field_name, id):
                self.last_id = id
                if field_name == "pdf":
                    raise RuntimeError("404 attachment missing")
                return b"%PDF-1.7 original submission"

        with patch("recommend_service.fulltext._openreview_client", return_value=(FakeClient(), {"authenticated": False})):
            content, receipt = _authenticated_openreview_pdf({"identifiers": {"openreview_id": "note-123"}})
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(receipt["attachment_field"], "originally_submitted_PDF")
        self.assertEqual(receipt["attachment_fallbacks"][0]["field_name"], "pdf")

    def test_fulltext_cooldown_failures_are_serially_requeued_once(self) -> None:
        papers = [
            {"title": "Rate Limited Paper One", "authors": ["Ada Lovelace"], "identifiers": {"openreview_id": "note-1"}},
            {"title": "Rate Limited Paper Two", "authors": ["Alan Turing"], "identifiers": {"openreview_id": "note-2"}},
        ]
        calls: dict[str, int] = {}

        def fake_acquire(paper, _paper_dir):
            identity = fulltext_module.paper_identity(paper)
            calls[identity] = calls.get(identity, 0) + 1
            if calls[identity] == 1:
                return {
                    "schema_version": fulltext_module.ACQUISITION_SCHEMA_VERSION,
                    "identity": identity,
                    "paper": paper,
                    "status": "temporarily_unavailable",
                    "full_text_available": False,
                    "retryable_after_cooldown": True,
                    "cooldown_services": ["openreview"],
                    "temporary_failure_reasons": ["http_429_rate_limited"],
                }
            return {
                "schema_version": fulltext_module.ACQUISITION_SCHEMA_VERSION,
                "identity": identity,
                "paper": paper,
                "status": "ready",
                "full_text_available": True,
            }

        with TemporaryDirectory() as temporary, \
                patch.object(fulltext_module, "acquire", side_effect=fake_acquire), \
                patch.object(fulltext_module, "cached", return_value=None), \
                patch.object(fulltext_module, "publish_cache", side_effect=lambda _paper, _directory, result: result), \
                patch.object(fulltext_module, "cooldown_remaining", return_value=0):
            result = fulltext_module.acquire_many(papers, Path(temporary), workers=16)
        self.assertEqual(result["full_text_ready_count"], 2)
        self.assertEqual(result["cooldown_requeue"]["eligible_count"], 2)
        self.assertEqual(result["cooldown_requeue"]["attempted_count"], 2)
        self.assertEqual(result["cooldown_requeue"]["recovered_count"], 2)
        self.assertEqual(result["cooldown_requeue"]["worker_count"], 1)
        self.assertTrue(all(count == 2 for count in calls.values()))

    def test_weaker_identity_contract_cache_is_not_reused(self) -> None:
        paper = {"title": "Contract Versioned Paper", "authors": ["Ada Lovelace"], "year": 2026}
        with TemporaryDirectory() as temporary, patch.object(fulltext_module, "FULLTEXT_CACHE_ROOT", Path(temporary)):
            cache_dir = Path(temporary) / fulltext_module.cache_key(paper)
            cache_dir.mkdir(parents=True)
            text_path = cache_dir / "full_text.txt"
            text_path.write_text("old unchecked text " * 100, encoding="utf-8")
            (cache_dir / "acquisition.json").write_text(json.dumps({
                "schema_version": 1,
                "full_text_available": True,
                "paper": paper,
                "text_path": str(text_path),
            }), encoding="utf-8")
            self.assertIsNone(fulltext_module.cached(paper))

    def test_claude_reader_uses_one_slot_per_paper_below_cap(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            pending = [{"identity": f"paper-{index}"} for index in range(6)]
            (run_dir / "reading_queue.json").write_text(json.dumps({"pending": pending}), encoding="utf-8")
            barrier = threading.Barrier(len(pending))
            lock = threading.Lock()
            active = 0
            peak = 0

            def fake_run(_claude, item, _timeout, _repair):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(timeout=2)
                with lock:
                    active -= 1
                return {"identity": item["identity"], "status": "complete"}

            with patch.object(claude_reads_module, "claude_status", return_value={"available": True, "path": "/fake/claude"}), patch.object(claude_reads_module, "_run_one", side_effect=fake_run):
                result = claude_reads_module.run_claude_reads(run_dir)
            self.assertEqual(result["worker_count"], len(pending))
            self.assertEqual(result["completed_count"], len(pending))
            self.assertEqual(peak, len(pending))
            self.assertTrue(result["pipeline_refill"])
            self.assertEqual(result["pipeline_batch_count"], 1)

    def test_claude_reader_hard_caps_and_refills_at_sixteen(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            pending = [{"identity": f"paper-{index}"} for index in range(20)]
            (run_dir / "reading_queue.json").write_text(json.dumps({"pending": pending}), encoding="utf-8")
            first_pool_ready = threading.Event()
            lock = threading.Lock()
            active = 0
            started = 0
            peak = 0

            def fake_run(_claude, item, _timeout, _repair):
                nonlocal active, started, peak
                with lock:
                    active += 1
                    started += 1
                    peak = max(peak, active)
                    if started == 16:
                        first_pool_ready.set()
                first_pool_ready.wait(timeout=2)
                with lock:
                    active -= 1
                return {"identity": item["identity"], "status": "complete"}

            with patch.object(claude_reads_module, "claude_status", return_value={"available": True, "path": "/fake/claude"}), patch.object(claude_reads_module, "_run_one", side_effect=fake_run):
                result = claude_reads_module.run_claude_reads(run_dir, workers=100)
            self.assertEqual(result["worker_count"], 16)
            self.assertEqual(result["max_concurrency"], 16)
            self.assertEqual(result["completed_count"], 20)
            self.assertEqual(result["pipeline_batch_count"], 2)
            self.assertEqual(peak, 16)

    def test_unavailable_claude_does_not_launch_reader_processes(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "reading_queue.json").write_text(json.dumps({"pending": [{"identity": "paper-1"}]}), encoding="utf-8")
            with patch.object(claude_reads_module, "claude_status", return_value={"available": False, "installed": True, "authenticated": False, "path": "/fake/claude", "reason": "claude_not_authenticated"}), patch.object(claude_reads_module, "_run_one") as run_one:
                result = claude_reads_module.run_claude_reads(run_dir)
            self.assertEqual(result["status"], "claude_unavailable")
            self.assertTrue(result["fallback_required"])
            run_one.assert_not_called()

    def test_claude_reader_accepts_model_and_per_paper_budget_controls(self) -> None:
        with TemporaryDirectory() as temporary:
            paper_dir = Path(temporary)
            prompt_path = paper_dir / "read_prompt.md"
            full_text_path = paper_dir / "full_text.txt"
            read_path = paper_dir / "read.md"
            receipt_path = paper_dir / "read_receipt.json"
            prompt_path.write_text("Read the paper", encoding="utf-8")
            full_text_path.write_text("paper text", encoding="utf-8")
            read_path.write_text("completed read", encoding="utf-8")
            receipt_path.write_text("{}", encoding="utf-8")
            item = {
                "identity": "paper-budget",
                "prompt_path": str(prompt_path),
                "full_text_path": str(full_text_path),
                "read_path": str(read_path),
                "receipt_path": str(receipt_path),
            }
            observed = {}

            def fake_subprocess(command, **_kwargs):
                observed["command"] = command
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")

            with patch.dict(os.environ, {"CLAUDE_MODEL": "low-cost-model", "CLAUDE_MAX_BUDGET_USD": "0.25"}, clear=False), \
                    patch.object(claude_reads_module.subprocess, "run", side_effect=fake_subprocess):
                result = claude_reads_module._run_one("/fake/claude", item, 60, "")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["model"], "low-cost-model")
            self.assertEqual(result["max_budget_usd"], "0.25")
            self.assertIn("--model", observed["command"])
            self.assertIn("low-cost-model", observed["command"])
            self.assertIn("--max-budget-usd", observed["command"])
            self.assertIn("0.25", observed["command"])

    def test_no_claude_fallback_is_exactly_three_balanced_batches(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            items = []
            for index in range(10):
                items.append({
                    "index": index + 1,
                    "identity": f"paper-{index}",
                    "full_text_available": True,
                    "text_path": str(run_dir / f"paper-{index}.txt"),
                    "text_chars": 2000,
                    "paper": {"title": f"Paper {index}", "authors": ["Author"], "venue": "ICLR", "year": 2026},
                })
            (run_dir / "full_text_results.json").write_text(json.dumps({"items": items}), encoding="utf-8")
            result = prepare_fast_batches(run_dir)
            self.assertEqual(result["batch_count"], 3)
            self.assertEqual(result["batch_sizes"], [4, 3, 3])
            self.assertEqual(sum(batch["paper_count"] for batch in result["batches"]), 10)
            self.assertTrue(all(Path(batch["manifest_path"]).is_file() for batch in result["batches"]))

    def test_fast_three_batch_finalization_does_not_require_per_paper_reads(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            shortlist = {"metadata_fingerprint": "metadata-v1", "papers": []}
            items = []
            for index in range(3):
                text_path = run_dir / f"paper-{index}.txt"
                text_path.write_text("Abstract Introduction Method Results References " * 40, encoding="utf-8")
                items.append({
                    "index": index + 1,
                    "identity": f"paper-{index}",
                    "full_text_available": True,
                    "text_path": str(text_path),
                    "text_chars": text_path.stat().st_size,
                    "paper": {"title": f"Paper {index}", "authors": ["Author"], "venue": "ICLR", "year": 2026},
                })
            (run_dir / "metadata.json").write_text(json.dumps({"metadata_fingerprint": "metadata-v1"}), encoding="utf-8")
            (run_dir / "shortlist.json").write_text(json.dumps(shortlist), encoding="utf-8")
            (run_dir / "full_text_results.json").write_text(json.dumps({
                "items": items,
                "metadata_fingerprint": "metadata-v1",
                "shortlist_fingerprint": stable_hash(shortlist),
            }), encoding="utf-8")
            batches = prepare_fast_batches(run_dir)
            batch_by_identity = {
                item["identity"]: batch["batch_id"]
                for batch in batches["batches"] for item in batch["items"]
            }
            cards = []
            for item in items:
                cards.append({
                    "identity": item["identity"], "title": item["paper"]["title"],
                    "batch_id": batch_by_identity[item["identity"]], "full_text_path": item["text_path"],
                    "summary": "summary", "decisive_evidence": ["evidence"], "limitations": ["limit"],
                    "borrowable_elements": ["method"], "confidence": "medium",
                    "match_score": 8, "transferability_score": 7, "final_score": 15,
                })
            evidence_path = run_dir / "evidence_cards.jsonl"
            evidence_path.write_text("\n".join(json.dumps(card) for card in cards) + "\n", encoding="utf-8")
            result = finalize(run_dir, evidence_path, 2)
            self.assertEqual(result["reading_mode"], "codex_fast_three_batches")
            self.assertEqual(result["actual_count"], 2)
            self.assertFalse((run_dir / "read.md").exists())

    def test_focused_plan_can_choose_a_narrow_scope_with_rationale(self) -> None:
        plan = {
            "research_question": "Which recent papers define agent memory evaluation?",
            "workflow": {"mode": "focused", "rationale": "A narrow terminology check needs current arXiv metadata first.", "stop_after": "metadata", "shortlist_target": 12},
            "request_scope": {"user_specified_time": False, "user_specified_channels": False, "as_of_date": "2026-07-22"},
            "channel_decisions": [
                {"channel": "arXiv", "decision": "include", "reason": "Current terminology source"},
            ],
            "sources": [{"type": "arxiv", "start_date": "2026-07-01", "end_date": "2026-07-22", "categories": ["cs.AI"]}],
        }
        validated = validate_plan(plan)
        self.assertEqual(validated["workflow"]["mode"], "focused")
        self.assertEqual(validated["workflow"]["shortlist_target"], 12)

    def test_user_disabled_claude_defaults_to_conversation_sticky_preference(self) -> None:
        plan = {
            "research_question": "Which papers should be read?",
            "workflow": {
                "mode": "focused",
                "rationale": "Test the conversation reading preference.",
                "stop_after": "reading",
                "reading_preference": "codex_fast",
                "user_disabled_claude": True,
            },
            "request_scope": {"user_specified_time": True, "user_specified_channels": True, "as_of_date": "2026-07-22"},
            "channel_decisions": [{"channel": "ICLR", "decision": "include", "reason": "Requested source"}],
            "sources": [{"type": "venue", "venue_id": "iclr", "years": [2026], "complete_catalog": True}],
        }
        workflow = validate_plan(plan)["workflow"]
        self.assertEqual(workflow["reading_preference_scope"], "conversation")
        self.assertTrue(workflow["conversation_reading_preference_locked"])

    def test_missing_workflow_keeps_comprehensive_default_scope(self) -> None:
        plan = {
            "request_scope": {"user_specified_time": False, "user_specified_channels": False, "as_of_date": "2026-07-22"},
            "channel_decisions": [
                {"channel": "NeurIPS/NIPS", "decision": "exclude", "reason": "test"},
                {"channel": "ICLR", "decision": "exclude", "reason": "test"},
                {"channel": "ICML", "decision": "exclude", "reason": "test"},
                {"channel": "arXiv", "decision": "include", "reason": "test"},
            ],
            "sources": [{"type": "arxiv", "start_date": "2026-07-01", "end_date": "2026-07-22"}],
        }
        with self.assertRaisesRegex(ValueError, "NeurIPS/NIPS"):
            validate_plan(plan)

    def test_child_run_links_parent_without_copying_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent"
            child = root / "child"
            parent.mkdir()
            child.mkdir()
            (parent / "run.json").write_text(json.dumps({"run_id": "parent-run", "status": "complete"}), encoding="utf-8")
            (parent / "final_ranking.json").write_text(json.dumps({"recommendations": []}), encoding="utf-8")
            with patch.object(cli_module, "create_run", return_value=child):
                result = cli_module.initialize_run(parent_run_dir=parent, mode="incremental", question="Now investigate evaluation datasets")
            continuation = result["continuation"]
            self.assertEqual(continuation["parent_run_id"], "parent-run")
            self.assertIn("final_ranking.json", continuation["available_parent_artifacts"])
            self.assertFalse((child / "final_ranking.json").exists())

    def test_partial_metadata_workflow_can_finish_cleanly(self) -> None:
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "metadata.json").write_text(json.dumps({"status": "complete", "papers": [{"title": "Paper"}]}), encoding="utf-8")
            result = finish_stage(run_dir, "metadata")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["completed_stage"], "metadata")

    def test_priority_cache_names_are_canonical(self) -> None:
        self.assertEqual(PRIORITY_VENUE_NAMES["sigkdd"], "KDD")
        self.assertEqual(PRIORITY_VENUE_NAMES["cvpr"], "CVPR")

    def test_original_release_and_parity_rules_cover_all_venues(self) -> None:
        cases = {
            "neurips": 2026, "iclr": 2026, "icml": 2026, "kdd": 2026,
            "sigir": 2026, "cikm": 2026, "www": 2026, "aaai": 2026,
            "iccv": 2025, "cvpr": 2026, "acl": 2026, "ijcai": 2026,
            "eccv": 2026, "emnlp": 2026,
        }
        for venue_id, expected_first in cases.items():
            years, _ = _venue_year_candidates({"venue_id": venue_id, "years": [2026]})
            self.assertEqual(years[0], expected_first, venue_id)

    def test_unreleased_archival_date_never_suppresses_live_channel_probe(self) -> None:
        years, reasons = _venue_year_candidates({"venue_id": "neurips", "years": [2026]})
        self.assertEqual(years[0], 2026)
        self.assertTrue(any("must still be probed" in reason for reason in reasons))

    def test_kdd_2026_backfills_and_records_effective_year(self) -> None:
        sample = [{"title": "A Complete KDD Paper", "abstract": "A" * 100, "authors": ["Author"], "url": "https://example.test", "year": 2025}]
        def fetch(candidate):
            if candidate["years"] == [2026]:
                raise RuntimeError("2026 channel not ready")
            return sample, {"adapter": "test", "status": "complete", "complete_catalog": True, "exhaustion_proof": "test"}

        with patch("recommend_service.metadata._fetch_venue_exact", side_effect=fetch) as mocked:
            rows, details = fetch_venue({"type": "venue", "venue_id": "kdd", "years": [2026], "complete_catalog": True})
        self.assertEqual(mocked.call_args.args[0]["years"], [2025])
        self.assertEqual(details["requested_years"], [2026])
        self.assertEqual(details["effective_years"], [2025])
        self.assertTrue(details["year_fallback"])
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
