# 架构与代码位置

> meeting-recorder 的模块划分与文件清单。要改代码、定位某个功能在哪个文件时读这里。

## 架构

```
索引页卡片 ──▶ http://127.0.0.1:8789/  （Hermes 内置浏览器打开）
                    │
        ┌───────────┴───────────┐
        │ 前端 static/index.html │  仿豆包：录音控制条 + 麦克风选择(iPhone/MacBook)
        │  · 文字记录 Tab        │  + 双Tab(文字记录/实时总结) + 历史任务列表
        │  · 实时总结 Tab        │  + 纪要/流水预览模态框
        └───────────┬───────────┘
                    │ REST (同源 /api/*)
        ┌───────────┴───────────┐
        │ 后端 server.py         │  FastAPI @8789，独立进程
        │  · engine.py           │  sox 录音 → ASR → 句子流 → 产物落盘
        │  · export_minutes.py   │  纪要导出 Obsidian + WIKI
        │  · asr/swift_asr_bin   │  macOS Speech 本地实时识别器
        └────────────────────────┘
```

## 代码位置

| 文件 | 职责 |
|---|---|
| `~/.agents/skills/meeting-recorder/server.py` | FastAPI 服务（/api/* + 静态页面） |
| `~/.agents/skills/meeting-recorder/engine.py` | 录音/转写引擎：sox → ASR → 句子 → 纪要 |
| `~/.agents/skills/meeting-recorder/export_minutes.py` | 纪要导出（知识库 `$MEETING_KNOWLEDGE_BASE` 下的 **meetings/**；未配置则只落本地 `records/`） |
| `~/.agents/skills/meeting-recorder/static/index.html` | 前端页面（仿豆包美工） |
| `~/.agents/skills/meeting-recorder/asr/swift_asr.swift` | Speech 框架识别器源码（改完需重编译） |
| `~/.agents/skills/meeting-recorder/asr/swift_asr_bin` | 预编译识别器二进制（engine 优先用） |
| `~/.agents/skills/meeting-recorder/asr/asr_macos.swift` | Speech 文件级识别器源码（SFSpeechURLRecognitionRequest，v1.0.5 吸收；⚠️ 不嵌 plist 不调 requestAuthorization，见 `changelog.md` 的 v1.0.5） |
| `~/.agents/skills/meeting-recorder/asr/asr_macos_bin` | 文件级识别器预编译二进制（source 兜底用，缺失时 `_speech_file_fallback` 自动编译） |
| `~/.agents/skills/meeting-recorder/asr/macos_speech.py` | Speech 兜底转写 CLI（dsh 蒸馏，独立可用） |
| `~/.agents/skills/meeting-recorder/vocab.json` | 热词表（由 `vocab_seed.py` 种子 ＋ 人事库 ＋ 知识库实体合并而来，不进 Git） |
| `~/.agents/skills/meeting-recorder/start.sh` | 启动脚本（端口检测 + 启动） |

产物目录（dsh 单目录机制，2026-08-27 用户定稿，替代旧 tmp→归档两段式）：
- 开始会议即创建 → `~/.agents/skills/meeting-recorder/records/YYYYMMDDNNN/`（如 20260827001，当天序号 001 起）
- **全程使用**：录音中过程文件（pcm/流水/清洗稿/纠错清单/asr_stderr）与最终产物（流水/清洗稿/纪要/metadata/materials）**同目录**；收口后**音频一律删除**（2026-09-13 定稿）
- 任务完成后**不再搬家**；主题由 LLM 提取写入 metadata.json `topic` 字段（历史列表显示用）
- 未正常收口（有流水但无 metadata.json）→ 视为"未收口任务"，可继续/归档/删除
- 每个任务产物：`会议记录-流水.md`（完整语音转文字流水，格式"时间段 文字"，如 `17:10:57-17:11:07 双方目前已经合作了两个年度`）
  + `会议纪要.md`（LLM 基于完整流水提炼的正式版纪要）
  + 清洗稿/纠错清单/存疑清单/实体核验/metadata/materials
- **录音一律不留（2026-09-13 用户定稿，比 09-09 版更彻底）**：**任何情况**都不在本地保留语音文件——纪要成功、纪要失败、`MEETING_SKIP_MINUTES=1` 全部删掉 pcm/wav/mp3。09-09 版曾在"纪要失败/跳过"时生成 mp3 兜底，**该兜底已取消**。
  - 纪要失败的补救走**流水文件**（步骤2 已可靠落盘）+ `/api/regen_minutes` 重新生成，**不需要音频**；
  - **中断录音也删（2026-09-13 用户定稿，无例外）**：录音被中途打断（sox 异常退出 / 进程被杀）时，`engine.discard_audio_files()` 就地删掉该任务的 pcm/wav/mp3；服务启动时 `server._startup_maintenance()` 还会扫掉上次崩溃遗留的全部 `会议录音.*`。
    - 代价：未收口任务**不能再"续录"旧音频**——`/api/continue` 只能从当前时刻往后录（已有**流水**照常保留，不受影响）。这是一条明确的取舍，不要再改回"留 pcm 供续传"。
