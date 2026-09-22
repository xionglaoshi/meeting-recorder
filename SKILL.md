---
name: meeting-recorder
description: "会议记录/录音转写/纪要时用：本地录音→实时转写→智能纪要→自动归档，入口 127.0.0.1:8789."
license: 个人非商用（见同目录 LICENSE）
metadata:
  version: 1.0.14
  author: xionglaoshi
  platforms: [macos]
  hermes:
    tags: [meeting, asr, speech, transcript, minutes, 会议记录, 转写, 纪要]
---

# 会议记录员（Meeting Recorder）

本地会议记录服务：**麦克风录音 → 实时语音转写 → 三级纠错 → 智能纪要 → 自动归档**。
前端仿豆包美工，转写主引擎**阿里百炼 qwen-audio-3.0-asr-flash-streaming**（2026-08-20 起由 paraformer 迁移而来）+ macOS Speech 本地兜底；纪要 LLM 用 DeepSeek。

> ⚠️ **两个模型是两个厂商、两件事，别搞混（2026-09-13 用户明确）**：
>
> | 环节 | 用谁 | 代码位置 |
> |---|---|---|
> | **语音识别（ASR）** | **只用阿里百炼**——主引擎 `qwen-audio-3.0-asr-flash-streaming`，云端不可用时降级 macOS Speech；`asr_mode: auto` 即"**云端优先**" | `engine.ASR_MODEL` / `qwen_asr.py` |
> | **语义整理（纪要、主题）** | **`deepseek-flash`**（DeepSeek 主账户；2026-09-13 起替代 `deepseek-v4-flash`） | `engine.LLM_MODEL` |
>
> **DeepSeek 不能做语音识别**，它只吃"流水文本"产出纪要；转写这段从不经过 DeepSeek。
> 用户此前横向测过多种语音识别模型，**最终定的就是阿里百炼那套，不要替换**。

## 归属与位置（2026-09-20 起）

- **唯一正本 = `~/.agents/skills/meeting-recorder/`**（公用资产区·跨 Agent 共用件）。
  Codex / Hermes / dsh 三家**各自目录下的副本已删除**，一律用这一份，**不要再在 `~/.codex/skills/`、`~/.hermes/skills/`、`~/.dsh/skills/` 里重建副本**。
- **按需启停（四家统一，不得自启）**：用户手动 `meeting-up`（= `~/.agents/bin/meeting-up.sh`，等价 `bash ~/.agents/skills/meeting-recorder/start.sh`）/ `meeting-down` 收掉；**禁止**写进 launchd / 定时任务 / 宿主启动钩子，**Agent 不得自行拉起**。
- **产物与数据都在这一个目录里**：会议记录 `records/YYYYMMDDNNN/`、热词表 `vocab.json`、本地人名索引 `references/人名职责索引.local.md`——三家会话共用同一份历史，别再往各家目录复制。
- 归属契约见 `~/.agents/README.md`；沿革与端口/接口见 LLM-WIKI [[memories/tools/meeting-server]]。

## 生命周期流水线（v2.0 架构，2026-08-20 重构）

```
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
│ 会前准备 │→│ 会中采集 │→│ 转录校对 │→│ 纪要生成 │→│ 归档交付 │
│ (Pre)   │  │ (Live)  │  │(Proof)  │  │ (Gen)   │  │(Deliver)│
└─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘
   可选         必须         必须         必须         必须
```

### 阶段 1 · 会前准备（Pre · 可选，随时可提交）
- **输入**：会议材料/附件路径（**任意时刻可提交**：开会前/会中/结束后）
- **提交方式**：
  - 前端"会议材料"输入框 + "＋提交材料"按钮（常驻，录音中也可用）
  - `POST /api/materials {paths: [...]}`（旁路端点，任何状态可用）
  - 开始录音时也可一并传 `materials` 数组
- **动作**：后台线程并行解析（pdf/docx/xlsx/txt/md → 文本），**立即返回不阻塞**；解析失败/损坏文件只记录警告，不崩溃
- **存储**：线程安全（锁），任意时刻追加；结束时复制进交付目录 `materials/` 子目录
- **使用**：纪要生成时自动注入作参考（防幻觉）；`/api/regen_minutes` 支持会后补充材料重生成
- **红线**：材料解析**永不阻塞主流程**（录音/转写/纪要）；材料与转写冲突时以转写为准

### 阶段 2 · 会中采集（Live · 必须）
- 录音（iPhone/MacBook 麦克风）+ 流式转写（**qwen-audio 云端主 / macOS Speech 本地兜底**）
- 流水实时写入 `会议记录-流水.md`（时间段+文字格式）
- 暂停/恢复插 `⏸/▶` 标记

### 阶段 3 · 转录校对（Proof · 必须 · v2.0 新增）
- **输入**：完整流水
- **动作**：三级纠错体系的 L2 层——清洗稿 + 纠错对照清单 + 存疑清单
- **红线**：三档置信（高置信改 / 中置信标 `[?待核]` / 数字姓名只标不改）；纠错不得变脑补

## 阶段 4 · 纪要生成（Gen · 必须）
- 输入：清洗稿 + 会议材料摘要 + 用户填写三要素（主题/参会/地点）
- 动作：LLM（`deepseek-flash`）按 `references/会议纪要质量标准.md` 生成
- **长文 MapReduce**（Scribble 吸收）：流水 >15000 字符自动分段→每段摘要→每4段合并→最终合成，长会议不丢内容
- **会议类型差异化**（Scribble MoM 吸收）：对外会议（政府/客户）突出风险预警+客户必记要点；对内会议补充各方态度+行动计划
- **说话人自动命名**（MeetMemo 吸收）：流水含"说话人N"时，LLM 结合人名职责索引映射真实人名
- **多模板**（Scribble 吸收）：`/api/regen_minutes?template=minutes|summary|bullets|analysis`
- **结构化输出**（minutes 吸收）：交付目录含 metadata.json（标题/句数/时长/asr/材料数）
- 红线：缺失要素留空待补，不编造；术语归一按字典

### 说话人识别能力边界（2026-08-20 用户纠正，必须遵守）

**真实能力边界**：
- **线下会议**（录音转写主场景）：最多输出**说话人N**（ASR 音色分离编号）或**无标签**——**不可能识别真名**，这不是 bug，是能力边界
- **腾讯会议**（线上导入）：转写若开启且通过**账号识别**，才可能带真名（如"张三"）；是否带取决于转写设置，**不默认期待**
- **import 外部转写稿**：保留稿子自带的"XX：内容"说话人前缀（有就留），没有不强求

**设计铁律**：
1. 前端流水：**有说话人标签就显示彩色标签，没有就不显示**（不渲染空标签）
2. 不要默认期待"识别出说话人真名"——线下会议最多是编号
3. 说话人自动命名（`/api/speakers`）是**可选增强**（LLM 结合人名职责索引建议），不是默认流程，不确定就保留原标签

> 🔴 **P0（用户告知，2026-09-13，禁止"修复"）**：纪要里把共用称呼**按上下文消歧成具体人名**（例如流水里只有"张总"、存疑清单同时列出「张伟 / 张明」两个候选时，模型据讨论内容填了「张伟」）——**这是特意设计的机制，要保留**。
> 它与"缺失留空、不得编造"并不冲突：留空针对的是**流水里根本没有的信息**；消歧针对的是**有候选、需要按领域/上下文判断**的场合。**不要把它当 bug 改成留空或保留原称呼。**

## 阶段 4.5 · 说话人后处理（F · 可选）
- `POST /api/speakers`：LLM 建议"说话人N"→真实人名映射（结合人名职责索引）
- `POST /api/speakers/apply`：用户确认后替换流水/清洗稿标签

## 阶段 5 · 归档交付（Deliver · 必须）
- 五步收口：终止录音 → 检查流水完整 → 生成纪要 → 收尾（主题写 metadata，目录不搬家）→ 返回通知
- **产物落位（三层，2026-08-20 确认 + 2026-08-27 单目录机制）**：
  - **主存档**（唯一权威源）：`meeting/<YYYYMMDDNNN>/`（日期+当天序号，如 `20260827001`，dsh 单目录机制）——**开始会议即创建，全程使用**：录音中过程文件（pcm/流水/清洗稿/纠错清单/asr_stderr）与最终产物（流水.md + 清洗稿.md + 纠错清单.md + 存疑清单.md + 会议纪要.md + metadata.json + materials/）**同目录，结束不再搬家**；**pcm/wav/mp3 一律不留**：收口删、录音中断也删、服务启动扫掉遗留（2026-09-13 定稿，详见 `references/architecture-and-pipeline.md`）；主题（LLM 提取）写入 metadata.json 的 `topic` 字段供历史列表显示
  - **同步① 知识库**：`$MEETING_KNOWLEDGE_BASE/meetings/YYYY-MM-DD-<主题>-会议纪要.md`（仅纪要；未设该变量则跳过同步）
  - **同步② Obsidian Inbox**：⛔ **已废弃（2026-09-17）**——Obsidian 弃用后不再有 Inbox 通道；`obsidian_inbox()` 恒返回 None，除非显式设 `MEETING_OBSIDIAN_INBOX`
  - **同步范围铁律**：只有「正式版会议纪要」同步到 WIKI；**流水/录音/清洗稿/纠错清单一律留在 meeting/ 主存档，不同步**

## 腾讯会议集成（2026-08-20 实测验证）

**入口（2026-09-13 起随本技能自持）**：`~/.agents/skills/meeting-recorder/tools/tencent_meeting/scripts/tencent_meeting.py`（CLI，v1.0.14）
> 原先指向 Trae 的 wemeet 插件目录，插件已不存在；该 CLI 已随服务迁入本技能目录，工具说明见同目录 `REFERENCE.md`。
> ⚠️ **该 CLI 是腾讯官方代码（无开源许可），已写进 `.gitignore`，不进 Git 仓库**——本机照常可用，分享仓库时不会带上它。
**Token**：`TENCENT_MEETING_TOKEN`（~/.codex/.env）

```bash
# 调用工具（token 从 .env 注入）
cd ~/.agents/skills/meeting-recorder/tools/tencent_meeting/scripts
TENCENT_MEETING_TOKEN=$(grep TENCENT_MEETING_TOKEN ~/.codex/.env | cut -d= -f2-) \
  python3 tencent_meeting.py tools/call '{"name":"<工具>","arguments":{...,"_client_info":{"os":"macos-26","agent":"codex","model":"deepseek-flash"}}}'
```

**关键工具链**：
1. `search_records`（q+from/to+file_type=all|transcript）→ 找到会议（含 record_file_id）
2. `get_transcripts_details`（meeting_id+record_file_id）→ 完整转写（body→minutes→paragraphs→sentences→words）
3. `get_smart_minutes` → 官方 AI 纪要（对比用）
4. `get_records_list`（start/end_time，**≤31天**）

**坑**：
- 时间格式必须 RFC3339 带秒（`2026-08-17T11:30:00+08:00`），`from`/`to` 不带秒会报"无法解析时间格式"
- `get_records_list` 时间范围不得超过 31 天
- 转写 JSON 是双层嵌套：顶层 `{status_code, headers, body}`，body 是**字符串**需再 `json.loads` → `body['minutes']['paragraphs']`
- 说话人在 paragraph 级（`speaker.nick_name`），句子在 `sentences[].words[].text`（拼接）
- **技能更新**：由 **TraeWork 插件市场**统一管理（插件 → wemeet → 更新）；不再手工解压替换
- 2026-08-17 之前旧版 v1.0.8 的 search_records 查询有兼容问题（查不到记录），升级 v1.0.14 后正常

**与会议记录员联动**（已实测）：
1. 腾讯会议转写稿 → 保存为原始稿 → `POST /api/import {file, title, attendees, location}` → 全链路优化（L2 校对+实体核验+质量标准纪要+归档双库）
2. 优化稿与官方智能纪要对比：我们的版本保留官方漏掉的关键决议（如"演示禁用真实企业名"合规要求）

### 一键后端能力（tx_meeting.py）

**触发**：用户在 Hermes 中说"从腾讯会议把 XX 会议的纪要整理好"（XX = 主题关键词/会议号/时间）

**执行**（Hermes 调用）：
```bash
cd ~/.agents/skills/meeting-recorder
python3 tx_meeting.py --keyword 产品评审 --title "产品评审会" --attendees "张三、李四"
# 或按会议号:  --code 259932425
# 或先列出:   python3 tx_meeting.py --list --days 30
```

**全自动链路**：search_records 找会议 → 拉完整转写（get_transcripts_details）→ 原始稿存 Downloads → **后端须已手动启动**（未起则脚本提示，不自动拉起）→ /api/import 全链路优化（L2校对+实体核验+质量标准纪要）→ **自动同步 WIKI meetings/ + Obsidian Inbox/** → 优化稿存 Downloads

**注意**：
- 会议日期自动从腾讯会议元数据注入标题（三要素时间真实，避免"2025年X月X日"占位）
- 会议未开云录制/转写（has_transcript_content=false）→ 脚本报错提示
- 时间范围默认回溯 60 天（--days 可调）
- **后端不自动拉起**（2026-09-17 用户定）：服务平时静默，未启动时脚本只报错并给出 `start.sh` 启动命令，需你手动执行
- 智能纪要（get_smart_minutes）用于对比，可选

## 可靠性加固（2026-08-20 实测后实施）

### 前后端衔接原则（用户确认，必须遵守）
- **前端刷新/关闭/重开，绝不能影响后端任务**（录音/转写在后端独立线程，前端只是轮询展示）
- 前端操作（开始/暂停/恢复）后**回读 /state 校验**，不一致时提示（不静默）
- 按钮点击后**禁用防重复**（直到后端确认）

### 进程管理
- 启动时清理残留 sox/swift 子进程（防多进程抢占麦克风/端口）
- atexit 钩子：优雅退出时杀子进程
- start.sh 优雅重启：先 POST /api/stop 停任务再杀进程（不丢数据）

### 断点续传（出错任务找回）
- `GET /api/broken_tasks`：列出未收口任务（有流水但无 metadata.json，单目录机制）
- `POST /api/continue {dir}`：复用目录继续录音（**从当前时刻往后录**——中断的 pcm 已按"不留语音"删掉；已有流水/句子保留）
- `POST /api/recover {dir?}`：中断任务直接归档（恢复句子→L2校对→纪要→双库导出）
- **前端历史管理合并**：成功任务（墨绿标签）→ 流水/纪要按钮；失败任务（暗红标签）→ 继续/归档按钮（2026-09-09：听录音按钮已删，与音频不保留策略配套）

### 已知坑
- 修代码时**不能 kill 正在录音的后端**（会丢任务）——用 start.sh 优雅重启
- build_vocab.py 重新生成会保留 meta（热词 ID 不丢）；字典更新后需重跑 sync_vocab.py

## When to Use

- 用户说"会议记录 / 录音转写 / 语音转文字 / 会议纪要 / 开会记录 / 记一下会议"
- 用户要求对已有音频文件（m4a/wav/pcm）转写或补生成纪要
- 用户提到 8789 端口、会议记录员、meeting-server 相关任务
- 触发后：服务**按需启动（无 launchd 自启）**；先探活 `curl -s http://127.0.0.1:8789/api/state`，未运行时 `bash ~/.agents/skills/meeting-recorder/start.sh`；入口 http://127.0.0.1:8789/

## 快速开始

> **按需启动（2026-09-13 起；此前 2026-09-02～09-11 曾用 launchd 自启，已废弃）**：
>
> 🔴 **2026-09-20 用户口径（四家统一）**：本服务**不开机自启、不后台常驻**——**只在用户明确要用的那一次**才拉起（公用一键 `meeting-up` / `meeting-down`，或本技能 `start.sh`）；**Agent 不得自行拉起**，禁止写进 launchd / 定时任务 / 宿主启动钩子。
> 服务**没有开机自启**，也没注册任何 launchd 服务。要用就先探活、没起再拉：

```bash
# 探活
curl -s http://127.0.0.1:8789/api/state        # {"state":"idle",...} = 正常

# 未运行 / 需要重启（优雅：先停录音任务 → 杀进程 → 重新接管端口）
bash ~/.agents/skills/meeting-recorder/start.sh

# 停止
bash ~/.agents/skills/meeting-recorder/stop.sh
```

> 解释器不写死：`start.sh` 走 `runtime.py` 探测（项目 `.venv` → 用户已有 AI Agent 环境 → 系统 Python），
> 依赖已并入 `~/.codex/venv`（公用副本可回退 `~/.hermes/venv` / `~/.dsh/venv`）。API key 由 `settings.py` 读环境变量或 `.env` 链。
> ⛔ **2026-09-20 用户明确口径：不得恢复开机自启 / 后台常驻**（下方「运维要点 §开机自启」只作历史存档，不要执行）。

## 结束会议五步流程（用户确认的完整交付流程）

> **正式版会议纪要必须遵循 `references/会议纪要质量标准.md`**（标题三要素 / 六要素信息区 / 开篇总概括 / 主题段落 / 附件 / 缺失留空）。LLM 提示词（engine.py `_make_minutes`）已按此标准编写。

点击"结束会议"后，`engine.stop()` 依次执行：

```
步骤1：终止录音，清理后台
       ├─ 停 sox、停 ASR 连接、关 Swift/云端
       └─ join 推流线程（注意：pump 线程内自动完成时不能 join 自己）

步骤2：检查会议流水记录被完整保存
       ├─ 正则校验 "HH:MM:SS-HH:MM:SS" 时间段行数 >= 内存句数
       ├─ ✓ 完整 → 日志确认
       └─ ⚠ 不完整 → last_error 记录（不阻断后续）

步骤3：基于完整流水 → LLM 提炼正式版会议纪要
       ├─ 独立执行，可重试，失败记录 last_error 不阻塞
       ├─ 成功后同步导出 Obsidian + WIKI
       └─ MEETING_SKIP_MINUTES=1 可跳过（测试/只看流水时用）

步骤4：收尾（单目录机制，2026-08-27：不再搬家）
       ├─ 目录自始至终为 meeting/YYYYMMDDNNN/，产物原地保留
       ├─ LLM 按纪要/流水提取主题（4~12字）→ 写回 metadata.json 的 topic 字段
       └─ 更新产物路径（同目录，无移动）

步骤5：返回交付信息 → 通知用户查看
       └─ /api/stop 返回 task_dir + files + last_error
```

设计要点：
- **流水生成和纪要生成是两个独立步骤**：步骤2 必须可靠完成（流水是核心产物），步骤3 可重试/可跳过，纪要卡住不阻塞流水收尾
- source 模式推流完毕自动走完整 stop 流程（`pump` 尾部检测 source 完成后调 `self.stop()`）
- 无纪要时归档主题用流水前 2000 字提取

## 核心 API

| 方法 | 端点 | 说明 |
|---|---|---|
| GET | `/api/state` | 状态（state/asr_mode/asr_error/summaries） |
| GET | `/api/devices` | 麦克风列表（CoreAudio 设备名，iPhone 优先；SwitchAudioSource 列出） |
| POST | `/api/start` | `{device, source?, asr_mode}` 开始录音（source=已有音频转写） |
| POST | `/api/pause` `/api/resume` `/api/stop` | 控制 |
| GET | `/api/sentences` | 句子流 + 实时总结 |
| GET | `/api/tasks` | 历史任务列表 |
| GET | `/api/preview?dir&file` | 预览产物 md→html |
| POST | `/api/regen_minutes` | 补生成/重生成会议纪要（读流水 → LLM → 覆盖写回） |

## 已知缺陷

**2026-09-13 已全部修复**（原记录见 `references/changelog.md`）：

1. ~~**stop() 同步阻塞整个服务**~~ → **已修**。阻塞调用改走 `run_in_threadpool`，事件循环不再被占死。实测：stop 跑 17 秒期间 `/api/state` 连续 20 次轮询全部 200、单次约 1.5ms。
2. ~~**export 文件名 bug**~~ → **已修**。`extract_title` 改为三级提取（六要素 `会议主题：` → 旧模板小节 → 首行标题），并去掉重复的"纪要"后缀。
3. ~~**前端显示录音时长 = 墙上跨度**~~ → **已修**。新增 `engine.audio_seconds()`：由**已捕获的 PCM 字节数**算真实音频时长（16kHz 单声道 int16 → 每秒 32000 字节），并**边采边存快照**——因为纪要成功后 PCM 会按"录音不保留"策略删除，快了照才不会回落到墙上时钟一直涨。实测 19.9s 音频报 `duration_sec: 19`。
   > 仍保留：**导出日期取的是导出日而非会议开始日**（跨午夜会议可能差一天）——**用户 2026-09-13 确认这样没问题，不要改**。
4. ~~**`/api/stop`、`/api/regen_minutes` 是同步请求**~~ → **已修**：两个接口默认**异步**（提交即返回 + `job` 字段报进度），`?wait=1` 保留同步行为。详见下节。

## 异步收尾契约（2026-09-13 起，改接口前必读）

`/api/stop` 与 `/api/regen_minutes` **默认异步**：

- **立即返回** `{"ok": true, "async": true, "state": "processing", ...}`（实测 stop 响应 ~3ms）
- 耗时的"停录→L2→实体核验→纪要→归档→双库导出"在**后台线程**跑（`engine.start_job()`）
- 进度与结果看 `GET /api/state` 的 **`job`** 字段：`{kind, running, ok, error, started_at, finished_at}`
- `?wait=1` 走旧的同步语义（请求等到结果再返回），脚本/调试可用

**三个必须一起遵守的点**：

1. **任何"POST 完就杀进程"的地方，都要先等 `state` 回到 `idle`**。`start.sh`（优雅重启）与 `stop.sh` 已改为轮询等待（默认最多 900s，`$MEETING_STOP_WAIT` 可调）——否则会在纪要还没写盘时把服务杀掉。
2. **自动收尾（source 模式推流完毕）也走 `start_job`**，这样它会被登记进 `job`、且期间再 POST `/api/stop` 会被 **409 拒绝**（否则整套流程会跑两遍、白烧 LLM——已实测踩到）。
3. **`mark_processing()` 只接受 `recording`/`paused`**；已经是 `processing` 一律拒绝，防重复触发。

## 运维要点

> 本节是原 `README.md` 里**只有运维才用得到**的部分（其余内容与本文件重复，2026-09-13 已合并后删除 README，保持"技能只认 SKILL.md"）。

### 外部依赖（macOS）

- **必需**：`sox`（麦克风采集 PCM 流）、`ffmpeg`（音频解码 / 转 mp3）→ `brew install sox ffmpeg`
- **可选**：`SwitchAudioSource`（在多个麦克风间切换默认输入）、`xelatex`（PDF 导出走 pandoc 时用；缺了自动回退 weasyprint，已验证可用）
- **解释器不用管**：`runtime.py` 自动挑一个依赖齐全的环境（本机命中 `~/.codex/venv`）；自检 `python3 runtime.py --doctor`

### 配置

**权威清单在 `settings.py` 顶部 docstring**（随代码更新，此处不复制全表）。最常用的几个：

| 变量 | 默认 | 用途 |
|---|---|---|
| `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` | — | **必需**：云端 ASR / 纪要 LLM |
| `MEETING_PORT` | `8789` | 监听端口 |
| `MEETING_RECORDS_DIR` | `<技能目录>/records` | 产物根 |
| `MEETING_SKIP_MINUTES` | — | 置 `1` 只转写不生成纪要（省 token，排查用） |
| `MEETING_KNOWLEDGE_BASE` / `MEETING_OBSIDIAN_INBOX` | 自动探测 | 配了才同步纪要 |
| `MEETING_VOCAB_PREFIX` | `meeting` | 百炼热词表前缀 |

读取顺序：**环境变量 > 本技能 `.env` > `~/.config/meeting-server/.env` > 各家 `.env`（`~/.codex` → `~/.hermes` → `~/.dsh`）> 公用兜底 `~/.agents/.env`**；
`.env` 链按序**合并**（靠前优先），所以技能内 `.env` 只写要覆盖的那几项即可。
**凭证类设置以文件为权威**——宿主应用（Codex）会把会话启动时的 `.env` 导出进 shell，
那份是历史快照，若让环境变量优先会静默盖住新值（此坑已踩过）。

### 排障

| 现象 | 处理 |
|---|---|
| 起不来 / 缺依赖 | `bash setup.sh`；或 `python3 runtime.py --check` 看缺什么 |
| 不知道在用哪个解释器、哪些配置生效 | `python3 runtime.py --doctor` |
| 转写错字多 | 查 `vocab.json` 是否含你们的专有名词 → `python3 build_vocab.py` 重建 |
| 云端 ASR 连不上 | 确认 `DASHSCOPE_API_KEY` 有效、百炼控制台已开通对应模型 |
| 纪要没生成 | 看 `logs/`；确认 `DEEPSEEK_API_KEY`；用 `MEETING_SKIP_MINUTES=1` 隔离 |
| 导出的 PDF 打不开 | pandoc 需 `xelatex`；缺则自动回退 weasyprint。**改动导出逻辑必须校验 `%PDF-` 魔数**——pandoc 在输出无 `.pdf` 扩展名时会产出 HTML 且退出码仍为 0 |
| 说话人没还原成真名 | 填 `references/人名职责索引.local.md` |
| 端口被占 | `MEETING_PORT=8790 bash start.sh` |

### 开机自启（⛔ 2026-09-20 起禁止；以下配方仅作历史存档）

服务**按需启动**，没有注册任何 launchd 服务。若日后要恢复自启：plist 必须指向 **`serve.sh`**，
**不能指 `start.sh`**（后者有"等 launchd 拉起"分支，会自己等自己 → 反复重启）；plist 的 `PATH`
要含 `/opt/homebrew/bin`，否则 `sox`/`ffmpeg` 找不到，表现为"能启动但录不了音"。

### 数据与许可

- 服务**只监听回环地址**，不对外暴露
- `records/`（真实录音纪要）、`logs/`、`.env`、`hr_etl.py`、`vocab.json`、`vocab_seed.py`、
  `references/*.local.md` 均在 `.gitignore` 中，不随仓库外发
- 许可：**仅限个人非商业使用**，详见同目录 `LICENSE`

### 测试演练纪律（2026-09-13 踩过，务必遵守）

**任何测试录音都会走完整链路，包括把纪要同步进真实知识库（`$MEETING_KNOWLEDGE_BASE/meetings/`）。**
所以测试前**必须**把同步目标重定向到临时目录，跑完再删：

```bash
mkdir -p /tmp/mt-test/kb
MEETING_KNOWLEDGE_BASE=/tmp/mt-test/kb bash start.sh
# 测完：停服务 → 删测试任务目录 records/YYYYMMDDNNN → 删 /tmp/mt-test
```

实测事故：有一次重启服务时**漏了这个环境变量**，测试纪要直接写进了真实
`WIKI/meetings/`，还往 `log.md` 追加了一条入库记录——两处都要手工回滚。
**重启服务时务必确认 env 带上了**。事后核对方法：WIKI `meetings/` 文件数、
`log.md` 行数是否与测试前一致。


## 参考文档（按需读取，不要一次全读）

| 文件 | 什么时候读 |
|---|---|
| `references/会议纪要质量标准.md` | **生成/修改纪要前必读**——标题三要素、六要素信息区、开篇总概括、主题段落、附件、缺失留空。engine 的提示词实时读它，改这个文件即改所有后续纪要 |
| `references/人名职责索引.md` | 说话人命名/简称消歧要查人名、职务、分管分工时 |
| `references/人名职责索引.local.md` | 本机私有版（含真实姓名细节，不入库） |
| `references/architecture-and-pipeline.md` | 要改代码、想知道模块职责与文件位置时 |
| `references/asr-and-model-notes.md` | 动 ASR 引擎 / 热词表 / 纠错体系 / LLM 模型选型前 |
| `references/frontend-notes.md` | 改前端 `static/index.html`，或查历史改造需求与实现状态时 |
| `references/changelog.md` | 追历史版本、查某个改动为什么这么做时 |
