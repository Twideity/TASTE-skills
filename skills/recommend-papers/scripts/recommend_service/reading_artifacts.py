from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .fulltext import FULLTEXT_CACHE_ROOT, cache_key
from .metadata import clean
from .storage import now_iso, read_json, safe_write_target, update_run, write_json, write_text


SECTIONS = ("摘要", "动机与核心创新", "方法", "实验结果", "优缺点总结")
READ_CONTRACT_VERSION = 3
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_ENGLISH_FUNCTION_WORDS = {"a", "an", "and", "are", "as", "by", "for", "from", "in", "is", "of", "on", "our", "that", "the", "this", "to", "we", "which", "with"}
_PROSE_LATEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_MARKDOWN_PROTECTED_SPAN_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)|(?<!\\)\$[^$\n]*?(?<!\\)\$")


def _fulltext_items(run_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(run_dir / "full_text_results.json", {})
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("full_text_results.json is missing")
    return [item for item in items if isinstance(item, dict) and item.get("full_text_available") is True]


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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _paper_dir(run_dir: Path, item: dict[str, Any]) -> Path:
    return run_dir / "papers" / f"{int(item.get('index') or 0):04d}"


def _read_cache(item: dict[str, Any]) -> Path:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    return FULLTEXT_CACHE_ROOT / cache_key(paper) / "reading"


def prepare_reads(run_dir: Path) -> dict[str, Any]:
    directory = safe_write_target(run_dir)
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
            restored.append({"identity": item.get("identity"), "read_path": str(target), "receipt_path": str(receipt_target), "cache": "hit"})
        else:
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
            source_abstract = _source_abstract(paper)
            pending.append({
                "identity": item.get("identity"),
                "paper_id": item.get("identity"),
                "title": (item.get("paper") or {}).get("title"),
                "source": clean(paper.get("venue") or paper.get("source")),
                "full_text_path": item.get("text_path"),
                "read_path": str(target),
                "receipt_path": str(paper_dir / "read_receipt.json"),
                "full_text_chars": item.get("text_chars"),
                "full_text_sha256": fingerprints["full_text_sha256"],
                "source_abstract": source_abstract,
                "source_abstract_sha256": _sha256_text(source_abstract),
            })
    payload = {"schema_version": 1, "status": "pending" if pending else "ready_for_validation", "ready_full_text_count": len(items), "restored_count": len(restored), "pending_count": len(pending), "restored": restored, "pending": pending, "generated_at": now_iso()}
    write_json(directory / "reading_queue.json", payload)
    update_run(directory, stage="single_paper_reading", counts={"full_text_ready": len(items), "reads_restored": len(restored), "reads_pending": len(pending)})
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
        return ["dedicated Codex subagent read_receipt.json is missing or invalid"]
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
    if audit.get("mode") != "dedicated_codex_subagent" or audit.get("subagent_used") is not True or audit.get("article_markdown_written") is not True:
        errors.append("each paper must be read by a dedicated Codex subagent")
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
    directory = safe_write_target(run_dir)
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
        text = read_path.read_text(encoding="utf-8")
        errors = validate_read(text, title, paper)
        full_text_path = Path(clean(item.get("text_path"))).expanduser()
        full_text = full_text_path.read_text(encoding="utf-8") if full_text_path.is_file() else ""
        receipt_path = _paper_dir(directory, item) / "read_receipt.json"
        receipt = read_json(receipt_path, None)
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
    payload = {"schema_version": 1, "read_contract_version": READ_CONTRACT_VERSION, "status": status, "required_count": len(items), "validated_count": len(validated), "failed_count": len(failures), "items": validated, "failures": failures, "generated_at": now_iso()}
    write_json(directory / "read_artifacts.json", payload)
    if status == "complete":
        write_text(directory / "read.md", _aggregate(validated))
        update_run(directory, stage="single_paper_reads_ready", counts={"full_text_ready": len(items), "single_paper_reads": len(validated)})
    else:
        update_run(directory, stage="single_paper_reads_blocked", status="blocked", counts={"full_text_ready": len(items), "single_paper_reads": len(validated), "read_failures": len(failures)})
    return payload
