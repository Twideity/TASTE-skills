# TASTE Skills

English | [中文](#中文说明)

This repository contains `recommend-papers`, a standalone Codex skill for exhaustive scholarly metadata discovery, dual-mode full-text reading, reranking, and evidence-backed recommendations.

The backend crawls and normalizes paper metadata, acquires and caches full text, runs TASTE-style external Claude readers by default, validates reading artifacts, and maintains resumable workflow state. Codex handles research scoping, source selection, title-and-abstract scoring, final ranking, and synthesis.

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

The user does not need to specify every setting. For a comprehensive review, omitted settings use the documented defaults. For a focused question or follow-up, Codex may choose a smaller justified scope, reuse prior run artifacts and caches, and stop as soon as the requested evidence is sufficient.

### 4. Enable Claude deep reading (optional)

Claude is optional. After installing and signing in to Claude CLI, the skill uses the first method by default: one fresh external Claude per paper, with at most 16 running simultaneously. For large reading jobs, prefer an inexpensive API because every paper requires a separate Claude call. Step 5 can verify whether Claude is available.

If you do not want to configure or pay for Claude, say:

```text
Do not use Claude for deep reading in this conversation. Use exactly three Codex subagents to read three balanced paper batches directly.
```

That choice remains active throughout the current Codex conversation until you explicitly ask to use Claude again. See [Choose the deep-reading method](#choose-the-deep-reading-method) for the exact behavior of both methods.

### 5. Run diagnostics (optional)

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

Defaults are fallbacks rather than minimums. Each turn may independently customize research mode, sources, dates, categories, shortlist/recommendation counts, cache policy, reading backend, scoring priorities, output language, and stopping stage. Follow-up directions can create child runs that link prior evidence without copying or mutating it.

- Metadata shortlist for full-text acquisition and deep reading: 100 papers.
- Final recommendations: 20 papers.
- Conferences: the latest single year with complete usable metadata for NeurIPS, ICLR, and ICML.
- Built-in conference coverage also includes SIGKDD/KDD, SIGIR, CIKM, AAAI, ICCV, WWW, CVPR, ACL, IJCAI, ECCV, and EMNLP. Conference metadata uses the corresponding registered production channel and is cached only after the complete title-and-abstract corpus is verified. A transient error leaves the requested year unresolved rather than selecting an older edition; acquisition resumes from saved progress.
- arXiv: the trailing six calendar months, inclusive, across `cs.AI`, `cs.LG`, `stat.ML`, `cs.CL`, `cs.CV`, `cs.IR`, `cs.RO`, `eess.SY`, `cs.MA`, and `cs.NE` unless the user explicitly supplies categories.
- Topic-adaptive sources: one to three additional sources selected by Codex.
- Full-text download workers: 8 globally. Different APIs and official hosts run concurrently; every channel has its own concurrency slots, request interval, and cooldown. Same-channel capacity is source-specific and can be overridden with `RECOMMEND_PAPERS_HTTP_CONCURRENCY`. DBLP defaults to one slot and rotates among its three official mirrors on network/5xx failures; a long `Retry-After` is deferred instead of freezing the run. OpenReview defaults to one shared slot and cannot exceed three; official-client login/API/attachment and direct PDF/HTML requests share this gate. A worker cannot wait indefinitely for a cooldown or occupied cross-process slot. Papers deferred by 429/403/challenge are labeled temporary, then retried once through a single recovery worker after the shared cooldown instead of being reported as having no PDF. ECCV archive and virtual-page routes derive the official main PDF while excluding supplements, posters, and slides.
- Reading defaults to one fresh external Claude CLI process per paper through a continuously refilled pool capped at 16 simultaneous processes. If Claude is unavailable or explicitly disabled, exactly three Codex subagents directly read three balanced paper batches without per-paper artifacts.

Users may override the shortlist count, final recommendation count, explicit sources, categories, date range, and ranking priorities in the request.

## Choose the deep-reading method

The skill supports exactly two deep-reading methods:

1. **External Claude per paper (default).** Each paper is assigned to a fresh external Claude CLI process that follows the original TASTE Reading contract. At most 16 Claude processes run simultaneously; each completion immediately starts the next queued paper. Every paper produces its own validated Chinese `read.md` and receipt, so this method provides the strongest per-paper audit trail.
2. **Three direct Codex batches.** Use this when Claude is unavailable or the user explicitly rejects the first method. All papers are divided as evenly as possible across exactly three Codex subagents. Each subagent directly reads every PDF in its assigned batch as quickly as practical. This mode does not run the per-paper Claude workflow and does not create per-paper `read.md` artifacts; it favors lower waiting time under Codex's three-subagent limit.

To disable the first method for the current Codex conversation, say:

```text
Do not use Claude for deep reading in this conversation. Use exactly three Codex subagents to read three balanced paper batches directly.
```

Chinese instructions such as `本次对话不要使用 Claude 精读，全部改用三个 Codex subagent 分批直接阅读` or simply `不要用第一种精读方法` have the same effect. This preference remains active across every later question, changed research direction, continuation, and child run in the same Codex conversation. Silence in a later turn does not restore Claude. To switch back, explicitly say `Use Claude for deep reading again for the rest of this conversation` or `后续重新使用 Claude 精读`. A separate new Codex conversation has no access to the preference from this one.

## Workflow

1. `doctor` verifies the Python version, dependencies, private paths, OpenReview mode, and backend health.
2. `migrate-metadata-cache` normalizes cache layout and removes legacy or unproven cache artifacts.
3. `init-run` creates one persistent run; follow-ups may pass a parent run, research mode, and new question.
4. Codex interprets the research request, resolves dates and channels, and writes `plan.json`.
5. `metadata` exhaustively acquires or reuses metadata and writes one receipt per source plus `metadata.json`.
6. Codex scores every paper using title and abstract only. `shortlist` validates complete score coverage and selects the top 100 by default.
7. `fulltext` downloads and validates every shortlisted paper it can acquire, preserving identity across fallbacks and caching successful artifacts.
8. Default mode runs `prepare-reads`, then `claude-reads`; up to 16 external Claude processes run at once and each completion immediately starts the next paper. Each paper writes a Chinese `read.md` and bound receipt.
9. `validate-reads` validates those Claude artifacts and permits one parallel fresh-Claude repair pass for failures.
10. If Claude is unavailable or forbidden, `prepare-fast-read-batches` creates exactly three balanced manifests and Codex sends exactly three direct batch-reading subagents; this skips the preceding per-paper artifact path.
11. Codex produces a full-text evidence card and new two-dimensional score for every acquired paper.
12. `finalize` validates complete evidence coverage and arithmetic, reranks all deeply read papers, and selects the top 20 by default.
13. Codex writes the cross-paper synthesis. `complete` verifies the final artifacts and marks the run complete.

The stages are composable: metadata-only, shortlist, full-text, reading, and recommendation endpoints are supported. The backend blocks only the requirements of the selected endpoint and prevents stale or mismatched evidence reuse.

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
│   └── <venue>/<year>.json
└── fulltext/<channel>/<paper-identity-hash>/
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
│   ├── channel-contracts.md
│   ├── research-modes.md
│   ├── scoring-rubric.md
│   └── service-contract.md
└── scripts/
    ├── paper_service.py
    └── recommend_service/
```

- `SKILL.md` controls Codex behavior and the mandatory workflow.
- `references/` contains defaults, channel ownership, multi-turn modes, scoring rules, and the service contract. `SKILL.md` loads each one only for the stage that needs it.
- `scripts/recommend_service/` is the deterministic crawler, cache, acquisition, validation, and state backend.
- Codex coordinates the workflow; external Claude processes produce default per-paper analyses, while the no-Claude fallback uses exactly three direct Codex batch readers.

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

### 4. 启用Claude精读（可选）

Claude不是必需依赖。安装并登录Claude CLI后，skill默认使用第一种精读方式：每篇论文启动一个全新的外部Claude，最多同时运行16个。大量精读时，每篇论文都会产生一次独立Claude调用，最好使用便宜的API。第5步可以检查Claude是否可用。

如果不想配置或付费使用Claude，直接说：

```text
本次对话不要使用 Claude 精读，全部改用三个 Codex subagent 分批直接阅读。
```

该选择会在当前整个Codex对话中持续生效，直到用户明确要求恢复Claude精读。两种方式的完整差异见[选择精读方式](#选择精读方式)。

### 5. 运行诊断（可选）

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

默认值只是未指定设置的兜底值，不是最低要求。每一轮都可分别调整研究模式、渠道、日期、分类、初筛/推荐数量、缓存策略、阅读后端、评分重点、输出语言和停止阶段。后续方向可以创建链接上一轮证据的子任务，不复制或修改上一轮产物。

- 进入全文下载和精读的元数据前100篇。
- 最终推荐前20篇。
- 会议为最新完整可用年份的 NeurIPS、ICLR、ICML。
- 内置会议还包括 SIGKDD/KDD、SIGIR、CIKM、AAAI、ICCV、WWW、CVPR、ACL、IJCAI、ECCV、EMNLP；分别使用对应注册渠道的正式全量路径，只有完整标题与摘要全集通过验证后才会写入年度缓存。临时限流或网络故障时，该年份保持“暂未解析”，不会被误判为不存在并回退旧年份；正式抓取可从已保存进度继续。
- arXiv 为最近六个日历月，默认分类包括 `cs.AI`、`cs.LG`、`stat.ML`、`cs.CL`、`cs.CV`、`cs.IR`、`cs.RO`、`eess.SY`、`cs.MA`、`cs.NE`。
- Codex根据主题增加1–3个渠道。
- 全文下载默认全局8个worker；不同API和官方主机可同时运行，各渠道独立限制并发、请求间隔和冷却。同渠道容量按来源配置，也可通过 `RECOMMEND_PAPERS_HTTP_CONCURRENCY` 覆盖。DBLP默认1个槽位，网络错误或5xx时会在三个官方镜像间切换；过长的 `Retry-After` 会延期而不是卡住整次运行。OpenReview默认只使用1个共享槽位，且无论如何不超过3；官方client登录/API/附件与PDF/HTML直链共用同一门控和冷却。单个worker不会无限等待冷却或被占用的跨进程槽位。因429、403或challenge延期的论文会标记为暂时失败，并在冷却后由1个恢复worker自动重试一次，不会被表述为“没有PDF”。ECCV归档页和virtual页可定位官方主PDF，并排除supplement、poster与slides。
- 默认每篇全文使用一个全新的外部Claude CLI进程，但同时最多运行16个；任一进程完成后立即补入下一篇。Claude不可用或用户明确禁用时，恰好启动三个Codex subagent，直接阅读平均分配的三批论文，不生成逐篇产物。

用户可在请求中覆盖初筛数量、最终推荐数量、渠道、分类、日期和排名侧重点。

## 选择精读方式

本skill只支持以下两种精读方式：

1. **逐篇外部Claude精读（默认）。** 每篇论文启动一个全新的外部Claude CLI进程，完全执行原TASTE Reading的单篇精读规范；同时最多运行16个Claude，任一完成后立即补入下一篇。每篇分别生成经过验证的中文`read.md`和回执，逐篇可审计性最强。
2. **三个Codex subagent直接分批阅读。** Claude不可用或用户明确拒绝第一种方式时使用。系统把全部待读论文尽量平均分成三份，恰好交给三个Codex subagent；每个subagent直接、尽快阅读自己那批PDF。此方式不执行逐篇Claude流程，也不生成逐篇`read.md`，目的是在Codex只能三路并发时减少等待时间。

如果不想使用第一种精读方式，推荐直接对Codex说：

```text
本次对话不要使用 Claude 精读，全部改用三个 Codex subagent 分批直接阅读。
```

说`不要用第一种精读方法`或`不要用Claude精读`也会触发同一设置。该要求默认对当前整个Codex对话持续有效，包括后续追问、切换研究方向、继续原run和创建child run；后面的消息没有再次提到Claude，不代表取消该设置。只有用户明确说`后续重新使用 Claude 精读`，Codex才可以恢复第一种方式。如果只想临时禁用一轮，必须明确说`仅这一轮不要用Claude精读`。新建的另一个Codex对话不会自动继承本对话偏好。

## 完整工作流程

1. `doctor` 检查Python版本、依赖、外部存储路径、OpenReview访问模式和后端健康状态。
2. `migrate-metadata-cache` 统一缓存布局并删除旧式或没有完整性证明的缓存。
3. `init-run` 在XDG state目录创建一个持久任务；追问可传入父任务、研究模式和新问题。
4. Codex理解研究要求，确定日期、主题、渠道和会议年份，并写入`plan.json`。
5. `metadata` 通过每个渠道唯一的正式路径全量抓取或复用元数据，生成逐来源回执和 `metadata.json`。
6. Codex仅根据标题和摘要为所有论文评分；`shortlist` 验证评分覆盖率并默认选择前100篇。
7. `fulltext` 获取并验证候选全文，保证fallback过程中的论文身份一致，并缓存成功产物。
8. 默认执行 `prepare-reads` 和 `claude-reads`；后者使用最多16个并发槽位流水执行，每完成一篇就启动下一篇，逐篇写中文 `read.md` 和哈希绑定回执。
9. `validate-reads` 验证Claude产物，并允许对失败论文进行一次全新Claude并行修复。
10. Claude不可用或被明确禁用时，改执行 `prepare-fast-read-batches`，生成恰好三份均衡清单，再由Codex启动恰好三个subagent直接批量阅读；此分支跳过逐篇产物流程。
11. Codex为每篇全文可用论文生成证据卡，并根据全文重新进行两维评分。
12. `finalize` 验证证据覆盖率和分数算术，重新排名所有深读论文，默认选择前20篇。
13. Codex撰写跨论文综合报告；`complete` 验证最终产物并把任务标记为完成。

元数据、初筛、全文、阅读和推荐都可以作为有意的结束阶段；后端只校验当前所选终点的必要条件，同时阻止过期或错配证据被复用。

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
│   └── <venue>/<year>.json
└── fulltext/<channel>/<paper-identity-hash>/
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
