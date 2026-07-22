# TASTE Skills

English | [中文](#中文说明)

This repository contains `recommend-papers`, a standalone Codex skill for exhaustive scholarly metadata discovery, full-text acquisition, dedicated per-paper deep reading, reranking, and evidence-backed recommendations.

The backend crawls and normalizes paper metadata, acquires and caches full text, validates reading artifacts, and maintains resumable workflow state. Codex handles research scoping, source selection, title-and-abstract scoring, full-paper analysis, final ranking, and synthesis.

## Quick start

### 1. Install

From the repository root, run:

```bash
python install.py
```

The installer finds Python 3.10+, creates a private virtual environment outside the repository, installs dependencies, registers the skill under `$HOME/.agents/skills`, and runs diagnostics. Existing installations are reused; conflicting installations are preserved as timestamped backups.

In Codex, use `/skills` or type `$recommend-papers`. Restart Codex if the installed skill does not appear.

### 2. Configure OpenReview

The installer creates the following file automatically. Open it and fill in your OpenReview account:

```text
$HOME/.agents/skills/recommend-papers/read.env
```

```dotenv
OPENREVIEW_USERNAME=your-account@example.com
OPENREVIEW_PASSWORD=your-password
```

Leave both values empty to use anonymous OpenReview access.

### 3. Use the skill

Start a new Codex task and ask, for example:

```text
$recommend-papers 调研能够提升具身智能体长期任务规划能力的论文，重点关注可迁移的方法和评测设计。
```

Or:

```text
$recommend-papers Find and deeply compare recent papers on retrieval-augmented agents for scientific discovery. Recommend 15 papers and explain which methods are most reusable.
```

The user does not need to specify time or sources. When both are omitted, the skill applies its mandatory defaults and lets Codex select additional high-value channels.

### 4. Run diagnostics (optional)

macOS:

```bash
PYTHON="$HOME/Library/Application Support/TASTE/recommend-papers/venv/bin/python"
SERVICE="$HOME/.agents/skills/recommend-papers/scripts/paper_service.py"
"$PYTHON" "$SERVICE" doctor
```

Linux:

```bash
PYTHON="${XDG_DATA_HOME:-$HOME/.local/share}/taste/recommend-papers/venv/bin/python"
SERVICE="$HOME/.agents/skills/recommend-papers/scripts/paper_service.py"
"$PYTHON" "$SERVICE" doctor
```

Windows PowerShell equivalent:

```powershell
$service = Join-Path $HOME ".agents\skills\recommend-papers\scripts\paper_service.py"
$python = Join-Path $env:LOCALAPPDATA "TASTE\recommend-papers\venv\Scripts\python.exe"
& $python $service doctor
```

Continue only when the output includes:

```json
{
  "status": "ok",
  "python_version_supported": true,
  "standalone": true
}
```

Inspect the reusable metadata cache:

```bash
"$PYTHON" "$SERVICE" cache-status
```

In PowerShell, use `& $python $service cache-status`.

## Default behavior

- Metadata shortlist for full-text acquisition and deep reading: 100 papers.
- Final recommendations: 20 papers.
- Conferences: the latest single year with complete usable metadata for NeurIPS, ICLR, and ICML.
- arXiv: the trailing six calendar months, inclusive, across `cs.AI`, `cs.LG`, `stat.ML`, `cs.CL`, `cs.CV`, `cs.IR`, `cs.RO`, `eess.SY`, `cs.MA`, and `cs.NE` unless the user explicitly supplies categories.
- Topic-adaptive sources: one to three additional sources selected by Codex.
- Full-text download workers: 8.
- Per-paper reading: one dedicated Codex subagent per acquired full text, using the maximum available Codex subagent concurrency.

Users may override the shortlist count, final recommendation count, explicit sources, categories, date range, and ranking priorities in the request.

## Workflow

1. `doctor` verifies the Python version, dependencies, private paths, OpenReview mode, and backend health.
2. `migrate-metadata-cache` normalizes cache layout and removes legacy or unproven cache artifacts.
3. `init-run` creates one persistent run under the XDG state root.
4. Codex interprets the research request, resolves dates and channels, and writes `plan.json`.
5. `metadata` exhaustively acquires or reuses metadata and writes one receipt per source plus `metadata.json`.
6. Codex scores every paper using title and abstract only. `shortlist` validates complete score coverage and selects the top 100 by default.
7. `fulltext` downloads and validates every shortlisted paper it can acquire, preserving identity across fallbacks and caching successful artifacts.
8. `prepare-reads` restores revision-matched reading caches and creates a queue for uncached papers.
9. Codex dispatches exactly one dedicated subagent per pending paper. Each subagent reads the exact full text and writes a Chinese single-paper `read.md` plus a cryptographically bound receipt.
10. `validate-reads` checks titles, required sections, Chinese content, formulas, lengths, full-text hashes, complete abstract-translation mapping, distinctness, and subagent audit fields. It then creates the aggregate `read.md`.
11. Codex produces a full-text evidence card and new two-dimensional score for every acquired paper.
12. `finalize` validates complete evidence coverage and arithmetic, reranks all deeply read papers, and selects the top 20 by default.
13. Codex writes the cross-paper synthesis. `complete` verifies the final artifacts and marks the run complete.

The backend blocks downstream stages when metadata coverage, score coverage, single-paper reads, evidence cards, paths, hashes, or upstream fingerprints are incomplete or stale.

## Storage and cache layout

The repository is treated as immutable at runtime.

Persistent intermediate state follows `XDG_STATE_HOME`:

```text
${XDG_STATE_HOME:-~/.local/state}/taste/recommend-papers/
├── runs/<UTC-run-id>/
└── state/
    ├── arxiv-staging/
    ├── biorxiv-staging/
    ├── http/
    └── metadata-cache-migration.json
```

Reusable article data follows `XDG_CACHE_HOME`:

```text
${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers/
├── metadata/
│   ├── arxiv/<category>/<YYYY-MM-DD>.json
│   ├── biorxiv/<YYYY-MM-DD>.json
│   └── conference/<venue>/<year>.json
└── fulltext/<paper-identity-hash>/
    ├── acquisition.json
    ├── downloads/
    ├── extracted/
    └── reading/
```

Override the application roots with absolute paths:

```bash
export RECOMMEND_PAPERS_STATE_DIR='/absolute/path/to/state'
export RECOMMEND_PAPERS_CACHE_DIR='/absolute/path/to/cache'
```

`RECOMMEND_PAPERS_DATA_DIR` remains a backward-compatible alias for the state root.

Cache policy can be `reuse`, `refresh`, or `only`. Current-day arXiv and bioRxiv shards are always provisional and refetched. Failed paginated ranges retain external staging checkpoints; successful ranges delete them after publishing daily shards.

## Repository structure

```text
environment.yml
install.py
requirements.txt
skills/recommend-papers/
├── SKILL.md
├── agents/openai.yaml
├── references/
│   ├── defaults.json
│   ├── migration-parity.md
│   ├── scoring-rubric.md
│   └── service-contract.md
└── scripts/
    ├── paper_service.py
    └── recommend_service/
```

- `SKILL.md` controls Codex behavior and the mandatory workflow.
- `references/` contains defaults, scoring rules, migration semantics, and the service contract.
- `scripts/recommend_service/` is the deterministic crawler, cache, acquisition, validation, and state backend.
- Codex coordinates the workflow and its dedicated subagents produce the validated per-paper analyses used for final ranking.

## License

Released under the [MIT License](LICENSE).

---

<a id="中文说明"></a>

# 中文说明

本仓库包含独立的 Codex 论文推荐技能 `recommend-papers`。它能够完成学术元数据发现、全文获取、逐篇深度阅读、全文后重新排名和带证据的最终推荐。

后端负责论文元数据抓取与标准化、全文获取与缓存、阅读产物验证以及可恢复的工作流状态管理；Codex负责研究范围理解、渠道选择、标题摘要初筛、逐篇全文分析、最终排名及综合汇报。

## 快速开始

### 1. 一键安装

在仓库根目录运行：

```bash
python install.py
```

安装器会自动寻找Python 3.10以上版本，在仓库外创建私有虚拟环境、安装依赖、注册到`$HOME/.agents/skills`并运行诊断。已有正确安装会直接复用；冲突安装会先保存为带时间戳的备份。

在Codex中使用`/skills`或输入`$recommend-papers`。安装后如未显示，请重启Codex。

### 2. 配置OpenReview账户

安装器会自动生成以下文件，打开它并填写OpenReview账户：

```text
$HOME/.agents/skills/recommend-papers/read.env
```

```dotenv
OPENREVIEW_USERNAME=your-account@example.com
OPENREVIEW_PASSWORD=your-password
```

两项留空时使用OpenReview匿名访问。

### 3. 使用skill

新建一个Codex任务，例如输入：

```text
$recommend-papers 调研能够提升具身智能体长期任务规划能力的论文，重点关注可迁移的方法和评测设计。
```

也可以明确要求数量、日期、渠道或评分重点：

```text
$recommend-papers 调研最近两年的多模态RAG论文，深读前50篇，最终推荐15篇，重点比较评测设计。
```

用户不必指定时间和渠道。两者都没有指定时，skill会应用强制默认范围，并让Codex选择额外的高价值渠道。

### 4. 运行诊断（可选）

macOS：

```bash
PYTHON="$HOME/Library/Application Support/TASTE/recommend-papers/venv/bin/python"
SERVICE="$HOME/.agents/skills/recommend-papers/scripts/paper_service.py"
"$PYTHON" "$SERVICE" doctor
```

Linux：

```bash
PYTHON="${XDG_DATA_HOME:-$HOME/.local/share}/taste/recommend-papers/venv/bin/python"
SERVICE="$HOME/.agents/skills/recommend-papers/scripts/paper_service.py"
"$PYTHON" "$SERVICE" doctor
```

Windows PowerShell等价命令：

```powershell
$service = Join-Path $HOME ".agents\skills\recommend-papers\scripts\paper_service.py"
$python = Join-Path $env:LOCALAPPDATA "TASTE\recommend-papers\venv\Scripts\python.exe"
& $python $service doctor
```

必须看到：

```json
{
  "status": "ok",
  "python_version_supported": true,
  "standalone": true
}
```

检查元数据缓存：

```bash
"$PYTHON" "$SERVICE" cache-status
```

PowerShell中使用`& $python $service cache-status`。

## 默认设置

- 进入全文下载和精读的元数据前100篇。
- 最终推荐前20篇。
- 会议为最新完整可用年份的 NeurIPS、ICLR、ICML。
- arXiv 为最近六个日历月，默认分类包括 `cs.AI`、`cs.LG`、`stat.ML`、`cs.CL`、`cs.CV`、`cs.IR`、`cs.RO`、`eess.SY`、`cs.MA`、`cs.NE`。
- Codex根据主题增加1–3个渠道。
- 全文下载默认8个worker。
- 每篇全文分配一个独立Codex subagent，并使用当前运行环境允许的最大subagent并发度。

用户可在请求中覆盖初筛数量、最终推荐数量、渠道、分类、日期和排名侧重点。

## 完整工作流程

1. `doctor` 检查Python版本、依赖、外部存储路径、OpenReview访问模式和后端健康状态。
2. `migrate-metadata-cache` 统一缓存布局并删除旧式或没有完整性证明的缓存。
3. `init-run` 在XDG state目录创建一个持久任务。
4. Codex理解研究要求，确定日期、主题和渠道，并写入 `plan.json`。
5. `metadata` 全量抓取或复用各来源元数据，生成逐来源回执和 `metadata.json`。
6. Codex仅根据标题和摘要为所有论文评分；`shortlist` 验证评分覆盖率并默认选择前100篇。
7. `fulltext` 获取并验证候选全文，保证fallback过程中的论文身份一致，并缓存成功产物。
8. `prepare-reads` 恢复与当前全文版本匹配的阅读缓存，对未缓存论文生成队列。
9. Codex为每篇待阅读论文分配一个独立subagent；subagent读取精确全文，写中文单篇 `read.md` 和哈希绑定回执。
10. `validate-reads` 检查标题、章节、中文、公式、长度、全文哈希、完整摘要翻译映射、论文间内容独立性和subagent审计字段，然后生成汇总 `read.md`。
11. Codex为每篇全文可用论文生成证据卡，并根据全文重新进行两维评分。
12. `finalize` 验证证据覆盖率和分数算术，重新排名所有深读论文，默认选择前20篇。
13. Codex撰写跨论文综合报告；`complete` 验证最终产物并把任务标记为完成。

任何来源覆盖、初筛评分、单篇阅读、证据卡、路径、哈希或上游fingerprint不完整时，后端都会阻止进入后续阶段。

## 中间状态与缓存

skill运行时将仓库视为只读。

中间任务状态使用 `XDG_STATE_HOME`：

```text
${XDG_STATE_HOME:-~/.local/state}/taste/recommend-papers/
├── runs/<UTC-run-id>/
└── state/
    ├── arxiv-staging/
    ├── biorxiv-staging/
    ├── http/
    └── metadata-cache-migration.json
```

可复用论文数据使用 `XDG_CACHE_HOME`：

```text
${XDG_CACHE_HOME:-~/.cache}/taste/recommend-papers/
├── metadata/
│   ├── arxiv/<category>/<YYYY-MM-DD>.json
│   ├── biorxiv/<YYYY-MM-DD>.json
│   └── conference/<venue>/<year>.json
└── fulltext/<paper-identity-hash>/
    ├── acquisition.json
    ├── downloads/
    ├── extracted/
    └── reading/
```

可以用绝对路径覆盖：

```bash
export RECOMMEND_PAPERS_STATE_DIR='/absolute/path/to/state'
export RECOMMEND_PAPERS_CACHE_DIR='/absolute/path/to/cache'
```

`RECOMMEND_PAPERS_DATA_DIR` 只作为state root的向后兼容别名保留。

缓存策略支持 `reuse`、`refresh` 和 `only`。当天arXiv和bioRxiv日分片永远是临时状态并在后续任务中重抓。失败的分页范围保留外部断点；成功发布日分片后删除断点。

## 开源协议

本项目采用 [MIT License](LICENSE)。
