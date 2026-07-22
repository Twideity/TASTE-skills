from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .fulltext import FULLTEXT_CACHE_ROOT, cache_key
from .metadata import clean
from .storage import now_iso, read_json, require_run, safe_write_target, stable_hash, update_run, write_json, write_text


SECTIONS = ("摘要", "动机与核心创新", "方法", "实验结果", "优缺点总结")
READ_CONTRACT_VERSION = 4
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_ENGLISH_FUNCTION_WORDS = {"a", "an", "and", "are", "as", "by", "for", "from", "in", "is", "of", "on", "our", "that", "the", "this", "to", "we", "which", "with"}
_PROSE_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_MARKDOWN_PROTECTED_SPAN_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)|(?<!\\)\$[^$\n]*?(?<!\\)\$")


def _reading_workflow(run_dir: Path) -> dict[str, Any]:
    plan = read_json(run_dir / "plan.json", {})
    metadata = read_json(run_dir / "metadata.json", {})
    if not isinstance(plan, dict) or metadata.get("plan_fingerprint") != stable_hash(plan):
        raise ValueError("plan.json changed after metadata acquisition; create a child run or rerun metadata")
    workflow = plan.get("workflow") if isinstance(plan, dict) and isinstance(plan.get("workflow"), dict) else {}
    if clean(workflow.get("stop_after")).lower() not in {"reading", "recommendation"}:
        raise ValueError("Plan does not authorize a reading stage")
    return workflow


def _fulltext_items(run_dir: Path) -> list[dict[str, Any]]:
    run_dir = require_run(run_dir)
    payload = read_json(run_dir / "full_text_results.json", {})
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("full_text_results.json is missing")
    if payload.get("status") not in {"complete", "complete_with_gaps"}:
        raise ValueError("full_text_results.json is not a completed acquisition pass")
    metadata = read_json(run_dir / "metadata.json", {})
    shortlist = read_json(run_dir / "shortlist.json", {})
    if payload.get("metadata_fingerprint") != metadata.get("metadata_fingerprint") or payload.get("shortlist_fingerprint") != stable_hash(shortlist):
        raise ValueError("full_text_results.json is stale because metadata or shortlist changed")
    ready = [item for item in items if isinstance(item, dict) and item.get("full_text_available") is True]
    if not ready:
        raise ValueError("No successfully acquired full texts are available for reading")
    return ready


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprints(item: dict[str, Any]) -> dict[str, str]:
    path = Path(clean(item.get("text_path"))).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Acquired full text is missing: {path}")
    pdf_path = Path(clean(item.get("pdf_path"))).expanduser() if clean(item.get("pdf_path")) else Path()
    full_text_sha256 = _file_sha256(path)
    pdf_sha256 = _file_sha256(pdf_path) if str(pdf_path) != "." else ""
    revision = hashlib.sha256(f"full_text_sha256={full_text_sha256}\npdf_sha256={pdf_sha256}\n".encode("ascii")).hexdigest()
    return {"full_text_sha256": full_text_sha256, "pdf_sha256": pdf_sha256, "content_revision": revision}


def _source_abstract(paper: dict[str, Any]) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    return clean(paper.get("abstract_en") or paper.get("abstract") or metadata.get("abstract_en") or metadata.get("abstract"))


def _accepted_pdf_url(item: dict[str, Any], paper: dict[str, Any]) -> str:
    attempts = item.get("attempts") if isinstance(item.get("attempts"), dict) else {}
    for attempt in attempts.get("pdf") or []:
        if isinstance(attempt, dict) and attempt.get("accepted") is True and clean(attempt.get("url")):
            return clean(attempt.get("url"))
    return clean(paper.get("pdf_url"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_label(item: dict[str, Any], paper: dict[str, Any]) -> str:
    source = clean(paper.get("venue") or paper.get("source") or item.get("source")) or "来源未提供"
    published = clean(paper.get("published") or paper.get("year") or item.get("published"))
    conference_key = re.sub(r"[^a-z0-9]+", "", source.lower())
    conference_keys = {"neurips", "nips", "iclr", "icml", "sigkdd", "kdd", "sigir", "cikm", "aaai", "iccv", "www", "cvpr", "acl", "ijcai", "eccv", "emnlp"}
    year_match = re.search(r"\b(?:19|20)\d{2}\b", published)
    source_date = year_match.group(0) if conference_key in conference_keys and year_match else published
    return f"{source} {source_date}".strip()


def _normalize_read_metadata(text: str, item: dict[str, Any]) -> str:
    """Normalize only deterministic front matter; never rewrite scientific prose."""
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    title = clean(paper.get("title") or item.get("title"))
    normalized = text
    if title:
        normalized = re.sub(r"(?m)^#\s+.*$", f"# {title}", normalized, count=1)
    normalized = re.sub(
        r"(?m)^- \*\*来源：\*\*.*$",
        f"- **来源：** {_source_label(item, paper)}",
        normalized,
        count=1,
    )
    normalized = re.sub(
        r"(?m)^- \*\*论文链接：\*\*.*$",
        f"- **论文链接：** URL：{_markdown_link('论文页面', paper.get('url') or item.get('paper_url'))}；PDF：{_markdown_link('PDF', _accepted_pdf_url(item, paper) or item.get('pdf_url'))}",
        normalized,
        count=1,
    )
    return normalized


def _normalize_read_artifacts(
    read_path: Path,
    receipt_path: Path,
    item: dict[str, Any],
    *,
    relocated: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Apply the upstream metadata repair and keep the machine receipt truthful."""
    text = read_path.read_text(encoding="utf-8", errors="replace")
    old_heading_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    old_heading = clean(old_heading_match.group(1)) if old_heading_match else ""
    normalized = _normalize_read_metadata(text, item)
    if normalized != text:
        write_text(read_path, normalized)
    receipt = read_json(receipt_path, {})
    if not isinstance(receipt, dict):
        receipt = {}
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    audit = receipt.get("deep_read_audit") if isinstance(receipt.get("deep_read_audit"), dict) else {}
    current_title = clean(paper.get("title") or item.get("title"))
    receipt_title = clean(receipt.get("title"))
    if receipt_title and _normalized_title(receipt_title) == _normalized_title(old_heading):
        receipt["title"] = current_title
    if clean(receipt.get("source")):
        receipt["source"] = clean(paper.get("venue") or paper.get("source") or item.get("source"))
    if relocated:
        receipt["article_markdown_path"] = str(read_path)
        audit["article_markdown_path"] = str(read_path)
    if normalized != text:
        audit["deterministic_metadata_normalization"] = True
        audit["read_sha256"] = _sha256_text(normalized)
    receipt["deep_read_audit"] = audit
    write_json(receipt_path, receipt)
    return normalized, receipt


def _paper_dir(run_dir: Path, item: dict[str, Any]) -> Path:
    return run_dir / "papers" / f"{int(item.get('index') or 0):04d}"


def _read_cache(item: dict[str, Any]) -> Path:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    return FULLTEXT_CACHE_ROOT / cache_key(paper) / "reading"


def _markdown_link(label: str, url: Any) -> str:
    value = clean(url)
    return f"[{label}](<{value}>)" if value.startswith("http") else "未提供"


def _deep_read_prompt(item: dict[str, Any]) -> str:
    source = clean(item.get("source")) or "来源未提供"
    source_label = _source_label(item, {})
    abstract_rule = (
        "把 source_abstract 的每个英文句子完整翻译成中文，不得概括、漏掉数字/结论/限制；专名和缩写可保留英文。"
        if clean(item.get("source_abstract")) else
        "来源摘要缺失；在回执中报告证据缺口，不得用元数据臆造摘要。"
    )
    return f"""你是 TASTE Reading 为这一篇论文启动的专用 Claude 精读进程。只处理本队列项，不得读取其他论文，也不得调用 Agent 或 Task。

允许读取：
- 全文：`{item.get('full_text_path')}`
- 本地 PDF（如存在）：`{item.get('local_pdf_path') or '未提供'}`
- 本任务说明：`{item.get('prompt_path')}`

只允许写入以下两个文件，不得在工作区或论文目录写其他文件：
- `{item.get('read_path')}`
- `{item.get('receipt_path')}`

必须从头到尾分块检查全文，不能只读摘要、开头或搜索片段。完成后 `evidence_chars` 必须不少于 {int(item.get('full_text_chars') or 0)}，并绑定给定 SHA-256。科学内容只写入 read.md；receipt 只写机器状态、哈希、路径和逐句摘要翻译映射。

read.md 必须严格使用：
# {item.get('title')}

- **来源：** {source_label}
- **论文链接：** URL：{_markdown_link('论文页面', item.get('paper_url'))}；PDF：{_markdown_link('PDF', item.get('pdf_url'))}

## 摘要
## 动机与核心创新
## 方法
## 实验结果
## 优缺点总结

内容规则：
- 摘要：{abstract_rule}
- 动机与核心创新：两段分别以“动机：”“核心创新：”开头，合计 200–250 个中文字符。
- 方法：只讲本文提出的机制，300–400 个中文字符；至少一个来自正文语境的 KaTeX 公式并紧接通俗解释。行内公式用 `$...$`，展示公式用独占行的 `$$` 包围，不用自定义宏。
- 实验结果：20–150 个中文字符，写清实验类型、关键比较和总体结果。
- 优缺点总结：20–100 个中文字符，同时指出优势与证据边界。
- 所有二级栏目必须是实质中文；不得把模板化段落复用于其他论文。

receipt JSON 必须满足：
- `status="complete"`, `paper_id="{item.get('paper_id')}"`, `title="{item.get('title')}"`, `source="{source}"`, `subagent_deep_read=true`, `article_markdown_path="{item.get('read_path')}"`。
- `abstract_sentence_map` 按 source_abstract 原句顺序提供 `source_sha256` 与 `translation_zh`。
- `deep_read_audit.mode="dedicated_claude_subagent"`, `subagent_used=true`, `status="complete"`, `text_path="{item.get('full_text_path')}"`, `evidence_chars` 覆盖全文，`article_markdown_path` 为上述 read.md，`article_markdown_written=true`。
- `deep_read_audit.full_text_sha256="{item.get('full_text_sha256')}"`, `source_abstract_sha256="{item.get('source_abstract_sha256')}"`，并在写完 read.md 后计算准确的 `read_sha256`。
"""


def prepare_reads(run_dir: Path) -> dict[str, Any]:
    directory = require_run(run_dir)
    workflow = _reading_workflow(directory)
    if clean(workflow.get("reading_preference")).lower() == "codex_fast":
        raise ValueError("Plan selects the three-Codex fast fallback; do not prepare Claude per-paper reads")
    items = _fulltext_items(directory)
    restored = []
    pending = []
    for item in items:
        paper_dir = _paper_dir(directory, item)
        paper_dir.mkdir(parents=True, exist_ok=True)
        target = paper_dir / "read.md"
        cache_dir = _read_cache(item)
        manifest = read_json(cache_dir / "manifest.json", {})
        fingerprints = _fingerprints(item)
        cached_md = cache_dir / "read.md"
        cached_receipt = cache_dir / "read_receipt.json"
        if isinstance(manifest, dict) and manifest.get("read_contract_version") == READ_CONTRACT_VERSION and manifest.get("content_revision") == fingerprints["content_revision"] and cached_md.is_file() and cached_receipt.is_file():
            write_text(target, cached_md.read_text(encoding="utf-8"))
            receipt_target = paper_dir / "read_receipt.json"
            write_json(receipt_target, read_json(cached_receipt, {}))
            _normalize_read_artifacts(target, receipt_target, item, relocated=True)
            restored.append({"identity": item.get("identity"), "read_path": str(target), "receipt_path": str(receipt_target), "cache": "hit"})
        else:
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
            source_abstract = _source_abstract(paper)
            queue_item = {
                "identity": item.get("identity"),
                "paper_id": item.get("identity"),
                "title": (item.get("paper") or {}).get("title"),
                "source": clean(paper.get("venue") or paper.get("source")),
                "authors": paper.get("authors") if isinstance(paper.get("authors"), list) else [],
                "published": clean(paper.get("published") or paper.get("year")),
                "paper_url": clean(paper.get("url")),
                "pdf_url": _accepted_pdf_url(item, paper),
                "local_pdf_path": clean(item.get("pdf_path")),
                "run_dir": str(directory),
                "full_text_path": item.get("text_path"),
                "read_path": str(target),
                "receipt_path": str(paper_dir / "read_receipt.json"),
                "prompt_path": str(paper_dir / "read_prompt.md"),
                "full_text_chars": item.get("text_chars"),
                "full_text_sha256": fingerprints["full_text_sha256"],
                "source_abstract": source_abstract,
                "source_abstract_sha256": _sha256_text(source_abstract),
            }
            write_text(Path(queue_item["prompt_path"]), _deep_read_prompt(queue_item))
            pending.append(queue_item)
    payload = {"schema_version": 1, "status": "pending" if pending else "ready_for_validation", "ready_full_text_count": len(items), "restored_count": len(restored), "pending_count": len(pending), "restored": restored, "pending": pending, "generated_at": now_iso()}
    write_json(directory / "reading_queue.json", payload)
    write_json(directory / "reading_mode.json", {"mode": "external_claude_per_paper", "selected_at": now_iso()})
    update_run(directory, stage="single_paper_reading", counts={"full_text_ready": len(items), "reads_restored": len(restored), "reads_pending": len(pending)})
    return payload


def prepare_fast_batches(run_dir: Path) -> dict[str, Any]:
    directory = require_run(run_dir)
    workflow = _reading_workflow(directory)
    explicit_fast = (
        clean(workflow.get("reading_preference")).lower() == "codex_fast"
        and (workflow.get("user_disabled_claude") is True or workflow.get("claude_unavailable") is True)
    )
    observed_unavailable = read_json(directory / "claude_read_results.json", {}).get("status") == "claude_unavailable"
    if not explicit_fast and not observed_unavailable:
        raise ValueError("Fast three-Codex reading requires an explicit no-Claude plan or a claude_unavailable receipt")
    items = _fulltext_items(directory)
    fulltext_fingerprint = stable_hash(read_json(directory / "full_text_results.json", {}))
    base, remainder = divmod(len(items), 3)
    sizes = [base + (1 if index < remainder else 0) for index in range(3)]
    batches = []
    cursor = 0
    for batch_index, size in enumerate(sizes, 1):
        assigned = []
        for item in items[cursor:cursor + size]:
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
            assigned.append({
                "identity": item.get("identity"),
                "title": paper.get("title"),
                "authors": paper.get("authors") if isinstance(paper.get("authors"), list) else [],
                "source": clean(paper.get("venue") or paper.get("source")),
                "published": clean(paper.get("published") or paper.get("year")),
                "paper_url": clean(paper.get("url")),
                "pdf_url": _accepted_pdf_url(item, paper),
                "full_text_path": item.get("text_path"),
                "full_text_chars": item.get("text_chars"),
                "source_abstract": _source_abstract(paper),
            })
        cursor += size
        manifest_path = directory / f"codex_fast_batch_{batch_index}.json"
        batch = {"batch_id": batch_index, "paper_count": len(assigned), "items": assigned, "result_path": str(directory / f"codex_fast_batch_{batch_index}_results.json")}
        write_json(manifest_path, batch)
        batches.append({**batch, "manifest_path": str(manifest_path)})
    payload = {
        "schema_version": 1,
        "status": "ready",
        "reading_mode": "codex_fast_three_batches",
        "paper_count": len(items),
        "batch_count": 3,
        "batch_sizes": sizes,
        "fulltext_results_fingerprint": fulltext_fingerprint,
        "authorization": "user_disabled_claude" if workflow.get("user_disabled_claude") is True else "plan_claude_unavailable" if workflow.get("claude_unavailable") is True else "observed_claude_unavailable",
        "batches": batches,
        "generated_at": now_iso(),
    }
    write_json(directory / "codex_fast_batches.json", payload)
    write_json(directory / "reading_mode.json", {"mode": "codex_fast_three_batches", "selected_at": now_iso()})
    update_run(directory, stage="codex_fast_batch_reading", counts={"full_text_ready": len(items), "codex_fast_batches": 3})
    return payload


def _section(text: str, name: str) -> str:
    match = re.search(rf"(?ms)^##\s+{re.escape(name)}\s*$\n(.*?)(?=^##\s+|\Z)", text)
    return match.group(1).strip() if match else ""


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text))


def _normalized_title(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"[^\w-]+", " ", text, flags=re.UNICODE)
    return " ".join(text.casefold().split())


def _english_words(value: Any) -> list[str]:
    return [word.casefold() for word in _ENGLISH_WORD_RE.findall(str(value or ""))]


def _translation_issue(value: str, source_english: str) -> str:
    chinese = _cjk_count(value)
    latin = len(re.findall(r"[A-Za-z]", value))
    if chinese < 4 or (latin and chinese / (chinese + latin) < 0.15):
        return "missing_substantive_chinese"
    source_words = _english_words(source_english)
    candidate_words = _english_words(value)
    if len(source_words) >= 10 and len(candidate_words) >= 10:
        windows = {tuple(source_words[i:i + 10]) for i in range(len(source_words) - 9)}
        if any(tuple(candidate_words[i:i + 10]) in windows for i in range(len(candidate_words) - 9)):
            return "copied_english_source"
    scrubbed = re.sub(r"https?://\S+|`[^`]*`|\$[^$]*\$", " ", value)
    for segment in re.split(r"(?<=[.!?])\s+|\n+", scrubbed):
        words = _english_words(segment)
        if len(words) >= 12 and _cjk_count(segment) <= 2 and sum(word in _ENGLISH_FUNCTION_WORDS for word in words) >= 2:
            return "long_english_prose"
    return ""


def _unresolved_prose_latex(value: str) -> bool:
    protected = [(match.start(), match.end()) for match in _MARKDOWN_PROTECTED_SPAN_RE.finditer(value)]
    return any(not any(start <= match.start() < end for start, end in protected) for match in _PROSE_LATEX_COMMAND_RE.finditer(value))


def _formula_structure_error(text: str) -> str:
    scrubbed = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`", "", text)
    opened = ""
    index = 0
    while index < len(scrubbed):
        if scrubbed[index] == "\\":
            index += 2
            continue
        if scrubbed[index] != "$":
            if opened == "$" and scrubbed[index] == "\n":
                return "inline formula is not closed on one line"
            index += 1
            continue
        marker = "$$" if scrubbed.startswith("$$", index) else "$"
        if opened and marker != opened:
            return "mismatched formula delimiters"
        opened = "" if opened else marker
        index += len(marker)
    return "unclosed formula delimiter" if opened else ""


def validate_read(text: str, title: str, paper: dict[str, Any] | None = None) -> list[str]:
    errors = []
    lines = text.splitlines()
    actual_title = lines[0].strip()[2:] if lines and lines[0].strip().startswith("# ") else ""
    if not actual_title or _normalized_title(actual_title) != _normalized_title(title):
        errors.append("first heading must exactly match the paper title")
    headings = re.findall(r"(?m)^##\s+(.+?)\s*$", text)
    if headings != list(SECTIONS):
        errors.append("second-level headings must be the five required sections in order")
    if not re.search(r"(?m)^- \*\*来源：\*\* .+", text):
        errors.append("source metadata line is missing")
    if not re.search(r"(?m)^- \*\*论文链接：\*\* URL：.+；PDF：.+", text):
        errors.append("paper URL/PDF metadata line is missing")
    sections = {name: _section(text, name) for name in SECTIONS}
    if any(not value for value in sections.values()):
        errors.append("every required section must contain text")
    paper = paper if isinstance(paper, dict) else {}
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    source_abstract = clean(paper.get("abstract_en") or paper.get("abstract") or metadata.get("abstract_en") or metadata.get("abstract"))
    fixed_abstract = clean(paper.get("abstract_zh") or metadata.get("abstract_zh"))
    issue = _translation_issue(sections["摘要"], source_abstract) if sections["摘要"] else ""
    if issue:
        errors.append(f"abstract translation quality failed: {issue}")
    if fixed_abstract and sections["摘要"].strip() != fixed_abstract.strip():
        errors.append("provided fixed Chinese abstract must be preserved verbatim")
    if _unresolved_prose_latex(text):
        errors.append("LaTeX commands remain outside math delimiters")
    formula_error = _formula_structure_error(text)
    if formula_error:
        errors.append(formula_error)
    for name, value in sections.items():
        chinese = _cjk_count(value)
        latin = len(re.findall(r"[A-Za-z]", value))
        if chinese < 4 or (latin and chinese / (chinese + latin) < 0.15):
            errors.append(f"section lacks substantive Chinese: {name}")
    motivation = sections["动机与核心创新"]
    if motivation and ("动机：" not in motivation or "核心创新：" not in motivation):
        errors.append("motivation section must contain 动机： and 核心创新： paragraphs")
    motivation_chars = _cjk_count(motivation)
    if motivation and not 200 <= motivation_chars <= 250:
        errors.append("motivation and innovation must contain 200-250 Chinese characters")
    method = sections["方法"]
    method_chars = _cjk_count(method)
    if method and not 300 <= method_chars <= 400:
        errors.append("method must contain 300-400 Chinese characters")
    if method and not re.search(r"\$(?:\$)?[^$]+\$(?:\$)?", method):
        errors.append("method must include at least one LaTeX formula and explain it")
    experiment_chars = _cjk_count(sections["实验结果"])
    if sections["实验结果"] and not 20 <= experiment_chars <= 150:
        errors.append("experiment results must contain 20-150 Chinese characters")
    pros_chars = _cjk_count(sections["优缺点总结"])
    if sections["优缺点总结"] and not 20 <= pros_chars <= 100:
        errors.append("strengths and limitations must contain 20-100 Chinese characters")
    if _cjk_count(text) < 400:
        errors.append("single-paper read.md is too short")
    return errors


def _validate_receipt(receipt: Any, item: dict[str, Any], read_path: Path, full_text: str) -> list[str]:
    errors = []
    if not isinstance(receipt, dict):
        return ["dedicated Claude read_receipt.json is missing or invalid"]
    audit = receipt.get("deep_read_audit") if isinstance(receipt.get("deep_read_audit"), dict) else {}
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    source_abstract = _source_abstract(paper)
    fingerprints = _fingerprints(item)
    if not source_abstract:
        errors.append("source English abstract is missing; cannot validate complete Chinese translation")
    if receipt.get("status") != "complete" or receipt.get("subagent_deep_read") is not True:
        errors.append("receipt must report complete dedicated deep reading")
    if clean(receipt.get("paper_id")) != clean(item.get("identity")) or _normalized_title(receipt.get("title")) != _normalized_title(paper.get("title")):
        errors.append("receipt paper identity/title mismatch")
    if not clean(receipt.get("source")):
        errors.append("receipt source is missing")
    declared_read_path = Path(clean(receipt.get("article_markdown_path"))).expanduser()
    if str(declared_read_path) == "." or declared_read_path.resolve(strict=False) != read_path.resolve(strict=False):
        errors.append("receipt article_markdown_path mismatch")
    if audit.get("mode") != "dedicated_claude_subagent" or audit.get("subagent_used") is not True or audit.get("article_markdown_written") is not True:
        errors.append("each paper must be read by its dedicated external Claude process")
    if clean(audit.get("full_text_sha256")) != fingerprints["full_text_sha256"]:
        errors.append("receipt full-text fingerprint mismatch")
    if clean(audit.get("source_abstract_sha256")) != _sha256_text(source_abstract):
        errors.append("receipt source-abstract fingerprint mismatch")
    if clean(audit.get("read_sha256")) != _sha256_text(read_path.read_text(encoding="utf-8")):
        errors.append("receipt read.md fingerprint mismatch")
    declared_text_path = Path(clean(audit.get("text_path"))).expanduser()
    expected_text_path = Path(clean(item.get("text_path"))).expanduser()
    if str(declared_text_path) == "." or declared_text_path.resolve(strict=False) != expected_text_path.resolve(strict=False):
        errors.append("receipt full-text path mismatch")
    if int(audit.get("evidence_chars") or 0) < len(full_text):
        errors.append("receipt does not attest inspection of the complete extracted full text")
    sentence_map = receipt.get("abstract_sentence_map") if isinstance(receipt.get("abstract_sentence_map"), list) else []
    source_sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", source_abstract) if part.strip()]
    translated = []
    if len(sentence_map) != len(source_sentences):
        errors.append("abstract translation must cover every source sentence")
    else:
        for source, row in zip(source_sentences, sentence_map):
            chinese = clean(row.get("translation_zh")) if isinstance(row, dict) else ""
            if not isinstance(row, dict) or clean(row.get("source_sha256")) != _sha256_text(source) or _translation_issue(chinese, source):
                errors.append("abstract sentence translation/fingerprint is invalid")
                break
            translated.append(chinese)
    if translated and re.sub(r"\s+", "", _section(read_path.read_text(encoding="utf-8"), "摘要")) != re.sub(r"\s+", "", "".join(translated)):
        errors.append("read.md abstract must equal the complete mapped Chinese translation")
    return errors


def _exact_duplicate_section_failures(validated_texts: list[tuple[str, str]]) -> dict[str, list[str]]:
    seen: dict[tuple[str, str], str] = {}
    failures: dict[str, list[str]] = {}
    for identity, text in validated_texts:
        for section in SECTIONS:
            normalized = re.sub(r"\s+", "", _section(text, section))
            if len(normalized) < 120:
                continue
            key = (section, normalized)
            other = seen.get(key)
            if other and other != identity:
                message = f"section {section} is exactly duplicated across distinct papers"
                failures.setdefault(other, []).append(message)
                failures.setdefault(identity, []).append(message)
            else:
                seen[key] = identity
    return failures


def _aggregate(items: list[dict[str, Any]]) -> str:
    output = ["# 论文精读", ""]
    for number, item in enumerate(items, 1):
        text = Path(item["read_path"]).read_text(encoding="utf-8").strip()
        lines = text.splitlines()
        title = lines[0][2:].strip()
        body = "\n".join(lines[1:])
        body = re.sub(r"(?m)^##\s+", "### ", body)
        output.extend([f"## {number:03d}. {title}", body.strip(), ""])
    return "\n".join(output).rstrip() + "\n"


def validate_reads(run_dir: Path) -> dict[str, Any]:
    directory = require_run(run_dir)
    workflow = _reading_workflow(directory)
    if clean(workflow.get("reading_preference")).lower() == "codex_fast":
        raise ValueError("Plan selects the three-Codex fast fallback; per-paper Claude validation is not applicable")
    items = _fulltext_items(directory)
    validated = []
    validated_texts = []
    candidates = []
    failures = []
    for item in items:
        paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
        title = clean(paper.get("title"))
        read_path = _paper_dir(directory, item) / "read.md"
        if not read_path.is_file():
            failures.append({"identity": item.get("identity"), "read_path": str(read_path), "errors": ["read.md is missing"]})
            continue
        receipt_path = _paper_dir(directory, item) / "read_receipt.json"
        text, receipt = _normalize_read_artifacts(read_path, receipt_path, item)
        errors = validate_read(text, title, paper)
        full_text_path = Path(clean(item.get("text_path"))).expanduser()
        full_text = full_text_path.read_text(encoding="utf-8") if full_text_path.is_file() else ""
        errors.extend(_validate_receipt(receipt, item, read_path, full_text))
        if errors:
            failures.append({"identity": item.get("identity"), "read_path": str(read_path), "errors": errors})
            continue
        candidates.append((item, title, read_path, receipt_path, text, receipt))
        validated_texts.append((clean(item.get("identity")), text))
    duplicate_failures = _exact_duplicate_section_failures(validated_texts)
    if duplicate_failures:
        bad = set(duplicate_failures)
        candidates = [candidate for candidate in candidates if clean(candidate[0].get("identity")) not in bad]
        for identity, errors in duplicate_failures.items():
            item = next((candidate for candidate in items if clean(candidate.get("identity")) == identity), {})
            failures.append({"identity": identity, "read_path": str(_paper_dir(directory, item) / "read.md"), "errors": errors})
    for item, title, read_path, receipt_path, text, receipt in candidates:
        cache_dir = safe_write_target(_read_cache(item))
        cache_dir.mkdir(parents=True, exist_ok=True)
        write_text(cache_dir / "read.md", text.rstrip() + "\n")
        fingerprints = _fingerprints(item)
        write_json(cache_dir / "manifest.json", {"schema_version": 1, "read_contract_version": READ_CONTRACT_VERSION, "identity": item.get("identity"), **fingerprints, "validated_at": now_iso()})
        write_json(cache_dir / "read_receipt.json", receipt)
        validated.append({"identity": item.get("identity"), "title": title, "full_text_path": item.get("text_path"), "read_path": str(read_path), "receipt_path": str(receipt_path), "cache_path": str(cache_dir / "read.md")})
    status = "complete" if len(validated) == len(items) and items else "blocked"
    payload = {"schema_version": 1, "read_contract_version": READ_CONTRACT_VERSION, "status": status, "required_count": len(items), "validated_count": len(validated), "failed_count": len(failures), "items": validated, "failures": failures, "fulltext_results_fingerprint": stable_hash(read_json(directory / "full_text_results.json", {})), "generated_at": now_iso()}
    write_json(directory / "read_artifacts.json", payload)
    if status == "complete":
        write_text(directory / "read.md", _aggregate(validated))
        update_run(directory, stage="single_paper_reads_ready", counts={"full_text_ready": len(items), "single_paper_reads": len(validated)})
    else:
        update_run(directory, stage="single_paper_reads_blocked", status="blocked", counts={"full_text_ready": len(items), "single_paper_reads": len(validated), "read_failures": len(failures)})
    return payload
