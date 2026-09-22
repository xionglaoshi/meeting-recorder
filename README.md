# meeting-recorder · 会议记录助手

**给 AI Agent 用的开会记录工具**：录下来 → 实时转写 → 自动纠错 → 实体核验 → 出会议纪要 → 归档。
对标豆包等产品的"会议记录"体验，但**完全跑在你自己的机器上**，用什么模型你说了算。

> 本仓库与字节跳动/豆包无任何关系，只是"同样的能力"。

它本质上就三件事，所以任何 agent 都能用：

| 组成 | 说明 |
|---|---|
| **一个本地 HTTP 服务**（`server.py`，默认 `127.0.0.1:8789`） | 录音、流式转写、纪要生成、记录归档；自带网页界面（`static/index.html`） |
| **一份技能说明**（`SKILL.md`） | 告诉 agent 什么时候用、怎么用、有哪些坑 |
| **一组脚本** | 词表构建、花名册导入、实体核验、腾讯会议导入、自检…… |

默认模型：**语音识别走阿里百炼**（`qwen-audio-3.0-asr-flash-streaming`），**语义/纪要走 DeepSeek**。
两者都需要你**自己的 API Key**；不想用这两家也能换（见 [§5 换模型](#5-凭证与模型)）。

---

## 1. 它能做什么

| 能力 | 说明 |
|---|---|
| 实时转写 | 边说边出字（流式 partial），带"正在说"的实时行 |
| 热词增强 | 把你的人名/公司名/专有词喂给 ASR（最多 2000 条），专有名词不再变同音字 |
| L1 清洗 | 去口水词、去重复、去语气词 |
| L2 校对 | 用模型对照热词纠正同音错字，**每一处改动都留痕**（`纠错对照清单.md`），可回查 |
| 实体核验 | 人名、公司名、项目名拿不准的，列成"待确认"问你，不硬猜 |
| 会议纪要 | 出结构化纪要（议题/结论/待办），可反复重生成 |
| 会议材料 | 会前/会中/会后随时丢材料（PDF/Word/Excel/PPT…），纪要生成时作为参考 |
| 补充信息 | 你随口的一句"这个别写进纪要"，会被当成上下文 |
| 归档 | 落本地 `records/`；可选同步进你的知识库（Obsidian/任何目录） |
| 腾讯会议导入 | 用腾讯会议开的会，可以拉回记录直接出纪要 |
| 全本地兜底 | 可选 macOS 本机语音识别，断网/省钱时用 |

## 2. 工作流

```
录音 ──► ASR 流式转写 ──► L1 清洗 ──► L2 校对（热词+模型，留改动清单）
                                   └──► 实体核验（人名/公司/项目，拿不准就问你）
                                              └──► 生成纪要 ──► 归档 records/ ──► （可选）同步知识库
```

## 3. 环境要求

- macOS 或 Linux（Windows 未测；录音用的是 ffmpeg，理论上能跑）
- Python ≥ 3.9（推荐 3.11）
- `ffmpeg`（录音）：`brew install ffmpeg`
- 一个**能上网**的环境（调用云端 ASR/LLM）——或用 macOS 本机识别 + 自备模型

## 4. 安装

```bash
git clone https://github.com/xionglaoshi/meeting-recorder.git
cd meeting-recorder
bash install.sh          # 想先看它干什么：bash install.sh --dry-run
```

`install.sh` 会：装依赖（建 `.venv`）、把模板铺出来（`.env`、`vocab_seed.py`、`hr_sources.py`）、建好 `records/` `roster/` `logs/`。**已存在的文件一律不覆盖。**

然后：

```bash
vim .env                                  # ① 填凭证（见 §5）
vim vocab_seed.py                         # ② 填你的人名/专有词（见 §6）— 不填也能跑
bash start.sh                             # ③ 启动（http://127.0.0.1:8789）
.venv/bin/python runtime.py --doctor      # ④ 自检：缺什么、连得通吗，一条条告诉你
bash stop.sh                              # 停
bash serve.sh                             # 前台运行（调试用）
python3 smoke_test.py                     # 内置冒烟测试
```

## 5. 凭证与模型

`.env`（照 `.env.example` 填）：

| 键 | 必需 | 用途 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ✅ | 阿里百炼：录音转写、热词表 |
| `DEEPSEEK_API_KEY` | ✅ | 语义识别、校对、纪要生成 |
| `TENCENT_MEETING_TOKEN` | 可选 | 腾讯会议记录导入 |
| `MEETING_LLM_MODEL` | 可选 | 换纪要模型（默认 `deepseek-flash`） |
| `DASHSCOPE_BASE_URL` | 可选 | 百炼专属部署/代理域名 |
| `MEETING_KNOWLEDGE_BASE` | 可选 | 知识库根目录（配了才同步纪要） |
| `MEETING_HR_SRC_DIR` / `MEETING_HR_DB` | 可选 | 花名册目录 / 人事库路径 |
| `MEETING_SKIP_MINUTES=1` | 可选 | 只转写不生成纪要（省额度，调试用） |

**想换成自己的模型**，三条路：

1. **换纪要/语义模型**（最常改）：`.env` 里设 `MEETING_LLM_MODEL=<模型名>`。
   - 名字以 `deepseek` 开头 → 走 `https://api.deepseek.com/v1`（用 `DEEPSEEK_API_KEY`）
   - 其他名字 → 走百炼的 OpenAI 兼容端点（用 `DASHSCOPE_API_KEY`），所以填 `qwen-plus`、`qwen-max` 之类即可
   - 想接第三方 OpenAI 兼容服务（本地 vLLM、其他云）：改 `engine.py` 的 `_chat()`（约 2193 行，20 行代码，`base` 与 `key` 两个变量）
2. **换语音识别**：`engine.py` 顶部 `ASR_MODEL`（默认 `qwen-audio-3.0-asr-flash-streaming`）；专属部署/代理用 `DASHSCOPE_BASE_URL`。要换成完全不同的厂商，替换 `qwen_asr.py` 里的 WebSocket 客户端即可（它就是"把音频流喂进去、把文本流拿出来"的一块）。
3. **不联网**：用 macOS 本机语音识别（`asr/`，见 [§10](#10-可选集成)），再配一个本地 LLM。

> 热词上限：`qwen-audio` 系列 2000 条；老 `paraformer` 只有 500 条。`sync_vocab.py --target-model <模型>` 可按目标模型裁剪。

## 6. 自建你自己的词典（**这一步决定准确率**）

ASR 听错人名几乎是必然的（同音字太多）。本工具的准确率主要靠三层：**热词（喂给 ASR）→ 同音变体（纠错）→ 简称消歧（"张总"是谁）**。三层都由一个种子文件驱动：

```bash
cp vocab_seed.example.py vocab_seed.py     # install.sh 已经帮你做了
```

`vocab_seed.py` 里按类别填：

```python
PERSONS  = ["张伟", "李娜", "王强"]                 # 人名
PARTNERS = ["星辰科技", "云图信息"]                 # 合作方/客户
PROJECTS = ["星辰二期", "天枢平台"]                 # 项目名
COMPANIES = ["某某实业集团有限公司"]                # 公司全称（长名会自动取简称）
# 同音变体：错写法 → 正确写法（L2 校对用它纠错）
VARIANTS = {"星辰": ["新辰", "星晨"], "张伟": ["张玮", "张纬"]}
```

然后就三步：

```bash
python3 build_vocab.py     # 合并 你的种子 + 人事库 + 知识库实体 → vocab.json
python3 sync_vocab.py      # 把 vocab.json 同步到 ASR 侧的热词表
python3 check_vocab.py     # 检查：多少条、缺哪些层、有没有明显漏项
```

其它能提升准确率的资料（都是可选的，有就用）：

| 资料 | 怎么接 |
|---|---|
| **花名册/通讯录** | 丢成 `roster/roster.xlsx`（或 `花名册.csv`、`通讯录.xlsx`；列名含 姓名/部门/职务/入职日期，中英文表头都认）→ `python3 hr_etl.py` |
| **你已有的复杂表格**（考核表、考勤表、日报表…） | `cp hr_sources.example.py hr_sources.py`，把文件名写进去 → `python3 hr_etl.py`。表头风格不一致也能吃（按关键字匹配列） |
| **人名职责索引**（"张总"到底是谁） | `references/人名职责索引.md` 是**模板**，复制成 `references/人名职责索引.local.md` 按格式填（它不进 Git） |
| **公司/项目清单** | 直接写进 `vocab_seed.py` 的 `COMPANIES` / `PROJECTS` |

## 7. 一场会跑完，产出什么

真实记录落在 `records/<日期><序号>/`（**不进 Git**）。结构看 `examples/示例会议-20260115001（虚构）/`（**全部虚构**，只为让你看懂）：

| 文件 | 作用 |
|---|---|
| `metadata.json` | 标题、参会人、时长、句数、生成时间 |
| `会议记录-流水.md` | 逐句流水（带序号/时间） |
| `会议记录-清洗稿.md` | 去口水词后的稿子 |
| `纠错对照清单.md` | 模型改了哪几处，可回查、可回滚 |
| `实体核验-待确认.md` | 人名/公司名拿不准的，列出来问你 |
| `存疑清单.md` | 语义存疑、需你确认的点 |
| `会议纪要.md` | **正式纪要**（议题/结论/待办） |
| `补充信息.md` | 你随口补充的上下文（可选） |

## 8. 什么不在 Git 里（重要）

这个仓库**只有代码和模板**。下面这些永远不进 Git（见 `.gitignore`），也不会出现在提交里：

```
.env                  凭证
vocab_seed.py         你的词表种子（真人名/真公司名）
hr_sources.py         你的花名册文件名/目录
vocab.json            构建产物
roster/               你的花名册
references/人名职责索引.local.md   真实姓名职责索引
records/  logs/       真实会议记录与日志
```

仓库自带 `check_repo_safety.py`，可以拿它自查"我要提交的东西里有没有内部信息"：

```bash
python3 check_repo_safety.py               # 扫本目录，列出疑似内部信息
```

## 9. 服务与开关

| 项 | 值 |
|---|---|
| 地址 | `http://127.0.0.1:8789`（`MEETING_PORT` 可改） |
| 网页界面 | 打开上面的地址即可；材料/补充信息/导入转写稿都在界面上 |
| 自检 | `.venv/bin/python runtime.py --doctor` |
| 解释器 | `$MEETING_SERVER_PYTHON` 显式指定；否则自动挑（见 `runtime.py`） |
| 后台常驻 | macOS 可用 launchd：`MEETING_LAUNCHD_LABEL=com.example.meeting-server bash start.sh` |
| 记录目录 | `$MEETING_RECORDS_DIR`（默认 `<本目录>/records`） |

## 10. 可选集成

**腾讯会议导入**（`tx_meeting.py` + `tools/tencent_meeting/`）
用腾讯会议开完会，可以直接拉回转写稿出纪要：先按 `tools/tencent_meeting/` 的说明配好 MCP 服务与 `TENCENT_MEETING_TOKEN`，然后

```bash
python3 tx_meeting.py --list                       # 列最近会议
python3 tx_meeting.py --keyword 产品评审 --title "产品评审会" --attendees "张三、李四"
python3 tx_meeting.py --import <会议id>            # 导入 → 自动走校对/核验/纪要
```

**macOS 本机语音识别**（`asr/`）
不联网、不花钱的兜底（准确率低于云端）。`asr/*.swift` 是源码，仓库里也带了 arm64 预编译产物；要自己编译：

```bash
bash asr/build.sh       # 需要 Xcode 命令行工具（swiftc）
```

## 11. 给 Agent 用

把这个目录放进你的 agent 能读到的技能目录（本机示例：`~/.agents/skills/meeting-recorder/`），
让 agent 读 `SKILL.md` 即可——里面写了什么时候该用它、服务怎么起、结果在哪、有哪些坑。

如果你用的是 **Hermes 桌面端**：装 [hermes-office-viewer](https://github.com/xionglaoshi/hermes-office-viewer) 后，可以在右侧栏直接开会议记录面板（`127.0.0.1:8789`）。

## 12. 常见问题

- **没有 DASHSCOPE_API_KEY 能跑吗？** 能跑起来，但转写会报错；用 `asr/` 的本机识别可离线，但纪要仍需一个 LLM。
- **人名还是识别错？** 往 `vocab_seed.py` 的 `VARIANTS` 加"错写法→正确写法"，重跑 `build_vocab.py` + `sync_vocab.py`。
- **"_总"到底是哪个总？** 填 `references/人名职责索引.local.md`，并按上下文让模型消歧——同音/同姓时它会列进"待确认"，**不硬猜**。
- **纪要太长/太短？** 换模型（`MEETING_LLM_MODEL`）或让你更擅长的模型来写；也可以 `/regen_minutes` 重生成。
- **中文乱码**：CSV 编码按 `utf-8-sig → utf-8 → gbk → gb18030` 自动兜底。
- **录音没声音**：`bash asr/build.sh` 之外先看 `runtime.py --doctor` 的设备检测；macOS 需在"系统设置 → 隐私与安全性 → 麦克风"里授权。

## 13. 许可

**MIT License** —— 见 [LICENSE](LICENSE)。第三方组件与服务见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

## 相关项目

- [hermes-desktop-beautify](https://github.com/xionglaoshi/hermes-desktop-beautify) —— Hermes 桌面端美化插件；
  它把本工具作为「会议记录」面板的运行时依赖（面板就是嵌 `127.0.0.1:8789` 这张页）。
- [hermes-office-viewer](https://github.com/xionglaoshi/hermes-office-viewer) —— Hermes 桌面端的文档预览插件。
