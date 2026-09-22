# ASR 引擎、热词字典与模型策略

> 语音识别与语义整理的实现细节：引擎选型与迁移史、qwen-audio 接入协议、
> 热词维护、三级纠错体系、实测对比与踩坑。**动手改 ASR/热词/模型前必读。**

## ASR 引擎（双引擎分层，2026-08-20 迁移 qwen-audio）

### 主引擎迁移（2026-08-20 用户确认）

**主引擎从 paraformer-realtime-v2 迁移到 qwen-audio-3.0-asr-flash-streaming**（阿里 2026-07-31 发布的新一代模型）：
- **官方建议**：文档明确"Paraformer 是较早一代 ASR 模型，建议迁移到 Fun-ASR 或 Qwen-ASR"
- **准确率**：中文工业场景错字率 7.8%（Artificial Analysis 1.7% 错字率第一）
- **热词 2000 上限**（paraformer 500）→ 全量字典 1096 词注入
- **上下文增强**（核心优势，paraformer 没有）：领域术语/会议信息注入 → 专名显著提升
- **增量流式**：逐字输出（paraformer 整句 20-30s 蹦）→ 前端实时流式展示

### 接入方式（qwen_asr.py，原生 WebSocket run-task 协议）
- **不是 dashscope Recognition API**（paraformer 专用），qwen-audio 用原生 WebSocket：
  `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`
- 协议：连接 → run-task JSON（model=params+vocabulary+input.context）→ task-started → **二进制 PCM 直接 send_bytes**（无帧头！）→ finish-task → result-generated 流 → task-finished
- **websocket-client 1.9.0 用 `send_bytes`**（不是 send_binary，旧版本才有）
- **热词格式**：`parameters.vocabulary = {"词": 权重}`（**对象 key-value，不是数组**！数组会 parse failed）；权重 1-5 或 50（超级热词）；≤4字词 weight 5，其余 4
- **上下文格式**：`input.context = [{"role":"user","content":[{"type":"input_text","text":"领域术语"}]}]`
- **结果事件**：`result-generated`，`payload.output.sentence`（text/begin_time/end_time；end_time 非 null = 完整句，null = 增量）
- **增量去重**：多 result 流文本会重叠 → engine on_partial 只接受更长文本（跨流去重）

### 实时流式输出（partial，2026-08-20 新增）
- qwen-audio 逐字增量 → engine `_partial_text` → /api/sentences 返回 `partial` 字段 → 前端底部"正在说"行（浅蓝+斜体+闪烁光标）
- 完整句出现 → 并入流水 → partial 清空
- 效果：边说边逐字显示（paraformer 时代 20-30s 才整句蹦）

### 热词维护（qwen 版）
```bash
cd ~/.agents/skills/meeting-recorder
python3 build_vocab.py        # 重新生成字典（保留 meta）
python3 sync_vocab.py --target-model qwen-audio-3.0-asr-flash-streaming  # 预编译热词（可选）
```
- **即时热词**（qwen_asr 每次请求内联传 `vocabulary` 对象）从 `_build_hotwords()` 生成：
  顺序 partner/company/project 优先（合作方/公司名最影响纪要），person 在后；上限 1200
- **注意**：`_build_hotwords` 曾因 person 优先占满 500 上限导致"信宏"被截断——已改 partner 优先 + 上限 1200

### LLM 模型策略（2026-09-13 用户指定 `deepseek-flash`）

| 环节 | 模型 | 原因 |
|---|---|---|
| **纪要生成**（流水→纪要） | **`deepseek-flash`**（DeepSeek 主账户） | 用户指定（2026-09-13）。快、质量足够（纠错已前置字典）、与百炼 ASR 解耦（百炼欠费不影响纪要） |
| **主题提取**（写入 metadata.topic） | `deepseek-flash` | 同上 |
| **语音转写**（ASR） | 百炼 `qwen-audio-3.0-asr-flash-streaming` | 现状主引擎（2026-08-20 由 paraformer 迁移）；百炼只做 ASR，不承担 LLM 消耗 |

- `engine.py` `LLM_MODEL = settings.get("MEETING_LLM_MODEL") or "deepseek-flash"`（可被 `/api/regen_minutes` 的 model 参数覆盖）
- `server.py` 的 `MODEL_PREFERENCE` 已把 `deepseek-flash` 排首位；`/api/models` 返回的 `default` 跟随 `LLM_MODEL`（避免"选着 A 实际用 B"）
- `_chat()` 按模型前缀选端点：`deepseek*` → `api.deepseek.com`（DEEPSEEK_API_KEY）；其他 → 百炼专属域名（DASHSCOPE_API_KEY）
- ⚠️ **`deepseek-flash` 是推理模型**：`reasoning_content` 会吃 completion 预算。预算给小了会出现"reasoning 占满、content 为空、finish=length"。`_chat()` 已按 finish=length 自动加预算重试（下限 1000、上限 32000），**改调用时别再给小 max_tokens**。

### 实测结论（2026-08-20 qwen-audio 迁移后对比）

| 引擎 | 专名准确率 | 实时性 | 备注 |
|---|---|---|---|
| **qwen-audio-3.0-asr-flash-streaming**（主引擎） | ✅✅✅ 最高（热词上千条 ＋ 上下文增强：人名与行业专有词基本全对） | ✅ 增量逐字流式 | 新一代模型（2026-07-31），官方建议替代 paraformer |
| 百炼 paraformer-realtime-v2（旧主引擎） | ✅✅（人名同音字大量出错，需靠热词与纠错层兜底） | ✅ 整句流式（20-30s 蹦） | 无上下文增强，热词仅 500 |
| macOS Speech 框架 | ❌（新红帽/红警/开派） | ✅ 分段实时(10s) | 本地兜底（免费·离线） |
| ~~faster-whisper large-v3~~ | ~~❌~~ | ~~❌ 非实时~~ | **已删除（2026-08-19）** |

### 推荐架构（已确认采纳：qwen-audio 优先 + Speech 兜底）

```
 ① qwen-audio-3.0-asr-flash-streaming  ← 主引擎（auto 默认，最准）
    增量流式·专名全对·热词1096+上下文增强
    失败（欠费/断网）自动降级 ↓
 ② macOS Speech 框架                   ← 兜底（免费·本地·实时）
    实时流水展示
```

- **主引擎**：qwen-audio-3.0-asr-flash-streaming（热词+上下文双保险，`auto` 模式优先）
- **兜底**：云端不可用时自动降级 macOS Speech（免费本地，够用于实时展示）
- **faster-whisper 已完全移除**（2026-08-19）
- 统一产物：无论引擎都输出 完整wav→mp3 + 流水md(时间段格式) + 纪要md，单目录 YYYYMMDDNNN（不搬家）
- 云端相对秒：qwen-audio 回调用 `begin_time`（毫秒→秒）+ `_cloud_rel_offset`（重建偏移补偿）

### 成本测算（qwen-audio-3.0-asr-flash-streaming）

| 场景 | 计算 | 费用 |
|---|---|---|
| 1 小时会议 | 3600s × 单价 | 与 paraformer 同级（<1 元/小时） |
| 每月 ≤10 小时 | 免费额度 36000秒/月 | **0 元** |
| 每月 15 小时 | 超 5h | ≈5 元 |

### Swift 识别器实现要点（踩坑记录）

1. **必须预编译成二进制**（`swiftc -O -o swift_asr_bin swift_asr.swift`）：`swift script.swift` 每次 JIT 编译 10-30s，会吃掉录音开头；二进制毫秒级启动
2. **必须 READY 握手**：脚本启动后 print "READY"，engine 等这行才开始喂 PCM，避免初始化期数据丢失
3. **必须分段识别**：Speech 框架在管道喂入模式下要 EOF 才 flush 结果，所以每累计 10 秒音频（SEGMENT_BYTES=160000）调 `request.endAudio()` 出 final，然后重建 recognitionTask 继续
4. **不能用三线程（reader→queue→pusher）**：实测跨线程写 Swift stdin 0 输出；必须单线程 pump（读 sox → 写 Swift stdin → 写 pcm）
5. 输入 16kHz mono s16le PCM（ffmpeg `-ac 1 -ar 16000 -f s16le -` 输出），Swift 内 int16→float32 转换

### 踩坑记录（务必记住）

- **MacBook 合盖无法录音**：`AppleClamshellState=Yes` 时 avfoundation 采不到声音（macOS 挂起音频会话）。测试必须开盖！
- **MacBook 内置麦克风（`:1`）实测录出静音（峰值 0）**：默认用 iPhone 麦克风（`:0`，放会议中间拾音最准），sox 加 `gain -l 6` 增益（+6dB + simple limiter）
- **百炼欠费（Arrearage）**：ASR 和对话模型都不可用，engine 会降级（录音仍保存 wav，充值后可用 source 模式补转写）；本地 Speech 引擎不受影响
- **source 模式要限流**：`time.sleep(0.10)` 每块，否则瞬间灌爆识别器丢句
- **流水时间戳必须用 Swift 输出的 rel_s**：`_start_swift_reader` 聚合时若用 `datetime.now()` 做 ts，流水会变成墙上时钟换算的错误秒数（如"第61289秒"= 17:01:29）；正确做法是 reader 解析 Swift JSON 的 `ts` 字段（相对秒），`_append_sentence` 再用录音开始时刻换算成"HH:MM:SS-HH:MM:SS"时间段
- **source 传裸 pcm 读不到**：ffmpeg 无法识别无头裸流，source 必须传 wav/m4a；测试用短素材（如 `-ss 20 -t 10` 切 10 秒）加快验证

## 专用词汇字典（v2.0 增强版，2026-08-19）

**文件**：`~/.agents/skills/meeting-recorder/vocab.json`（build_vocab.py 生成）
**数据源**（信息源铁律：知识库 → WPS → 外部）：知识库内的人事档案（`entities/people/` 之类：在职名册 + 职务/部门；明细库按需重建为 /tmp/hr_data.db，见 `hr_etl.py`）为主；你自己的「简称/别名矩阵」做补充；组织架构与任命文件提供管理层分管分工。

**三类纠错能力**：

| 错误类型 | 机制 | 示例 |
|---|---|---|
| 同音不同字 | pinyin 变体自动生成 + homophone 专项层 | "niu che he"→牛车河；"红警/鸿景/弘景"→宏景贸易 |
| 同义词 | variants 数组 | 牛车河项目 = 牛车河水库项目/牛车河清淤项目 |
| 简称消歧 | disambiguation 层 ＋ `references/人名职责索引.md`（照模板填你自己的） | "张总"→按上下文判断是张伟还是张明；填得越全越准 |

### 三级纠错体系（2026-08-20 补齐 L1，三层全通）

**关键认知**：paraformer 是纯 ASR 模型，**不知道你的字典**——热词必须通过 `vocabulary_id` 显式传入才能纠错。8 组同音词只是 L3 的 LLM 提示词补充，**真正的源头预防是 L1 热词表**。

| 层 | 机制 | 状态 | 说明 |
|---|---|---|---|
| **L1 源头预防** | 百炼定制热词表（vocabulary_id） | ✅ 已接线 | company/project/business/industry/partner 全称+口语简称 **425 词**，weight=4，target=paraformer-realtime-v2 |
| **L2 转录校对** | proofread.py 变体映射 + 三档置信 | ✅ | 清洗稿 + 纠错清单 + 存疑清单 |
| **L3 纪要归一** | vocab_prompt() 全量注入 LLM | ✅ | 8 组高危同音词 + 简称消歧规则 |

**L1 热词维护**（字典更新后必做）：
```bash
cd ~/.agents/skills/meeting-recorder
python3 sync_vocab.py --target-model qwen-audio-3.0-asr-flash-streaming
# 全称 + 口语简称变体 → 百炼热词表（幂等更新，免费）
```
- 词源：vocab.json 的 company/project/business/industry/partner（**person 人名不进**——406 人挤占 500 限额且人名词频低）
- 超长词（>15字符）用 LONG_TERM_ALIAS 简称替代（如"罗田百纳"）

> ⚠️ **现状提醒（2026-09-13 核对代码后订正）**：转写主引擎 qwen-audio **不使用**百炼侧的热词表 id，
> 而是由 `engine._build_hotwords()` 从 `vocab.json` **直接内联注入**（`hotwords=` 参数，上限 2000 词）。
> 因此本节的 `sync_vocab.py` 属**遗留/可选**步骤：它写回的 `meta.asr_vocabulary_id` 只被
> `smoke_test.py` 断言"存在"，**转写链路并不读取**。真正影响转写准度的是 `vocab.json` 本身
> （改词表后跑 `build_vocab.py` 即生效，无需 sync）。`vocab.json` 里 `asr_target_model` 仍是
> `paraformer-realtime-v2`（上次同步时留下的值），不影响现有链路。

**维护**：
- 重新生成：`python3 ~/.agents/skills/meeting-recorder/build_vocab.py`（需 pypinyin）
- 高危同音词：编辑 build_vocab.py 的 HOMOPHONE 数组
- 消歧候选：自动从 hr_data.db 职务/部门生成；高管分管详情见 `references/人名职责索引.md`
- **组织架构调整后**：重读 WPS 聘任文件更新人名职责索引，再重跑 build_vocab.py
- **校验流程（新增实体后必做）**：
  1. WIKI/Obsidian 先建实体（公司/项目/合作方/人名）
  2. 重跑 `python3 ~/.agents/skills/meeting-recorder/build_vocab.py`（**建议用 venv python**：`~/.codex/venv/bin/python`，含 pypinyin 拼音变体；生成后**自动挂接 check_vocab 自检**，硬污染/权威源缺失会提示）
  3. 需要时单独跑校验：`python3 ~/.agents/skills/meeting-recorder/check_vocab.py`（退出码 0=通过）
     - 校验脚本权威源：employer-entities.md + LLM-WIKI 全站文件名/目录（Obsidian）+ hr_data.db
     - hr_data.db **按需自动重建**：脚本缺失时自动调 `~/.agents/skills/meeting-recorder/hr_etl.py`（服务自持；`~/.codex/scripts/hr_etl.py` 已成指针存根）读 WPS 员工档案材料，幂等写 `/tmp/hr_data.db`
     - 只报真问题：company/project/partner term 无法匹配权威源、homophone term 非法、disambiguation 人名不在人事库
     - ⚠️ 行业通用词（砂石/码头/国企等）不校验——它们不是企业专属实体
  4. 发现"变体当规范词"类错误（如"鑫宏"当 term）→ 立即修正 build_vocab.py 后重生成
- **术语归一铁律**：字典"规范词"必须来自权威数据源（WIKI/Obsidian 实体），不能手写；HOMOPHONE 只维护"拼音→规范词"映射，不定义规范词本身

**注入链路**：`vocab_prompt()` → 纪要 LLM 提示词（术语归一 + 同音词纠正 + 简称消歧规则）

