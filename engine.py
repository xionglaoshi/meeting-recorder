"""meeting-recorder 录音转写引擎：sox 麦克风 → 本地/云端 ASR → 句子流 + 产物落盘

状态机: idle → recording ⇄ paused → processing → idle

ASR 双引擎（可切换）：
- local: macOS Speech 框架（Swift 脚本流式，本地免费，默认）
- cloud: 阿里百炼 qwen-audio-3.0-asr-flash-streaming（需充值，source 或实时）

线程模型：
- 读线程: 读 sox stdout → queue
- 推流线程: queue → ASR + 写 pcm（paused 或 queue 空时推静音帧保活）
- 句子聚合: 本地 ASR 输出增量 partial → 按"文本停止增长超时"落盘句子
- callback: 云端 ASR WS 线程 → 句子落盘
"""
import datetime
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback

import settings

# ── 常量 ──
# 接入点不写死：默认官方公共端点，用专属部署的人在 .env 里配 $DASHSCOPE_BASE_URL（见 settings.py）
WS_URL = settings.dashscope_ws_url()
ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"
LLM_MODEL = settings.get("MEETING_LLM_MODEL") or "deepseek-flash"   # 纪要默认模型（用户指定；可被 /regen_minutes 覆盖）
VOCAB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vocab.json")
_vocab_cache = None


def _build_hotwords(max_n=1200):
    """从字典生成即时热词列表（person/partner/project/company 的 term+变体）。
    优先短词（<=6字，人名/简称），超长公司全称用简称。
    顺序：partner/company 优先（合作方/公司名最影响纪要准确），person 在后。
    qwen-audio 支持最多 2000 热词（paraformer 只 500）。"""
    try:
        v = load_vocab()
        words = []
        # 合作方/公司/项目优先（易错且重要）；person 人名次之（人名词频低但热词空间够）
        for cat in ("partner", "company", "project", "business", "industry", "person"):
            for it in v.get("categories", {}).get(cat, []):
                term = (it.get("term") or "").strip()
                if term and 2 <= len(term) <= 8:
                    words.append(term)
                for var in (it.get("variants") or []):
                    var = (var or "").strip()
                    if var and 2 <= len(var) <= 8:
                        words.append(var)
        seen = []
        for w in words:
            if w not in seen:
                seen.append(w)
        return seen[:max_n]
    except Exception:
        return []


def load_vocab():
    """加载专属词汇字典（懒加载 + 容错）"""
    global _vocab_cache
    if _vocab_cache is None:
        try:
            with open(VOCAB_PATH, encoding="utf-8") as f:
                _vocab_cache = json.load(f)
        except Exception as e:
            print(f"[engine] 字典加载失败: {e}", flush=True)
            _vocab_cache = {}
    return _vocab_cache


def vocab_prompt():
    """把字典压缩为 prompt 段（供纪要 LLM 术语归一 + 消歧）。

    精简版（2026-08-26）：只保留「人名同音异形字变体 + 高危同音词 + 简称消歧」三类关键纠错，
    去掉公司/项目/business/industry/partner 全量变体（L2 校对已把变体替换为规范词，纪要阶段
    无需重复注入）+ 拼音变体（ASR 热词专用）+ 纯称谓（X总/X经理，消歧规则已覆盖）。
    否则 system prompt 达 19k 字，触发推理模型对超长输入偶发 content 为空
    → 纪要 0 字节（2026-08-26 实测复现）。"""
    v = load_vocab()
    cats = v.get("categories", {})
    lines = []
    # ① 人名同音异形字变体（仅中文、非纯称谓、非拼音）——纪要阶段人名纠错核心
    for it in cats.get("person", []):
        vs = [x for x in (it.get("variants") or [])
              if x and x != it["term"] and len(x) >= 2
              and not x.isascii()                          # 排除拼音/英文
              and not x.endswith(("总", "经理", "工", "董", "助理", "总监", "董事长"))]  # 排除纯称谓
        if vs:
            lines.append(f"person:{it['term']}={('、'.join(vs))}")
    # ② 同音词专项层——高危同音词直接纠正
    homo = cats.get("homophone", [])
    if homo:
        lines.append("\n【同音词纠正】以下拼音/错误写法应改为规范词：")
        for h in homo:
            lines.append(f"  {h['pinyin']}（{'、'.join(h['variants'])}）→ {h['term']}")
    # ③ 消歧规则——共享简称按上下文判断
    dis = cats.get("disambiguation", [])
    if dis:
        lines.append("\n【简称消歧】以下简称对应多人，根据会议讨论内容判断具体是谁：")
        for d in dis:
            cand = "；".join(
                f"{c['name']}（{c['position']}，负责{('/'.join(c['domains']) if c['domains'] else c['dept'])}）"
                for c in d["candidates"])
            lines.append(f"  {d['alias']} → {cand}")
        lines.append("  判断方法：提到与候选人分管领域相关的话题时，指向对应候选人；无法确定则保留原称呼")
    return "\n".join(lines)


def get_vocab_id():
    """读取 ASR 热词表 vocabulary_id（sync_vocab.py 创建后写回）"""
    return load_vocab().get("meta", {}).get("asr_vocabulary_id", "")
# 任务根 = 服务目录/records（所有 AI Agent 共享产物）；可用 $MEETING_RECORDS_DIR 覆盖
MEETING_ROOT = str(settings.RECORDS_DIR)
SAMPLE_RATE = 16000
CHUNK = 3200                       # 100ms @16k mono int16
SILENCE = b"\x00" * CHUNK          # 暂停保活静音帧
IDLE_STOP = 60                     # 服务端静音超时（s），靠心跳/静音帧对抗

# ── 录音采集器：sox（2026-08-26 从 ffmpeg avfoundation 迁移）──
# 根因：ffmpeg avfoundation 输入设备在 macOS 27 上采样掉帧（dsh 实测），音频不连续、ASR 识别率低。
# sox 直接走 CoreAudio 原生 API，规避 avfoundation 掉帧 bug，音质更清晰。
# 设备选择：sox 用 `-d`（CoreAudio 默认输入设备），切换靠 SwitchAudioSource 改系统默认输入。
# 增益：sox `gain -l 6`（+6dB + simple limiter 防削波）。
# 注意：sox 无 `limiter` 滤镜（那是 ffmpeg alimiter 概念），防削波用 gain 的 `-l`（simple limiter）参数。
VOLUME_FILTER = "volume=3.0,alimiter=limit=0.95"   # 仅 source 文件解码模式仍用（ffmpeg 解码不涉及 avfoundation 采集）
SOX_GAIN = ["gain", "-l", "6"]
DEFAULT_DEVICE = ""


def load_api_key():
    """百炼（DashScope）API key：环境变量优先，其次 settings 的 .env 链。"""
    return settings.require("DASHSCOPE_API_KEY")


def load_ds_api_key():
    """DeepSeek API key（纪要 LLM 主账户）"""
    return settings.require("DEEPSEEK_API_KEY")


def make_task_dir():
    """会议专属目录：records/YYYYMMDDNNN（日期+当天序号；records=~/.agents/skills/meeting-recorder/records，公用产物根 2026-09-20）。
    开始会议即创建，所有过程文件（pcm/流水/清洗稿/纠错清单）与最终产物（纪要/metadata/materials）
    全程放同一目录，结束时不再搬家。序号 001 起（对齐 dsh mkSessionDir）。"""
    os.makedirs(MEETING_ROOT, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    n = 1
    while os.path.exists(os.path.join(MEETING_ROOT, f"{today}{n:03d}")):
        n += 1
    d = os.path.join(MEETING_ROOT, f"{today}{n:03d}")
    os.makedirs(d, exist_ok=True)
    return d


AUDIO_FILENAMES = ("会议录音.pcm", "会议录音.wav", "会议录音.mp3")


def discard_audio_files(task_dir, log=True):
    """删除任务目录里的语音文件（"录音一律不留"策略，2026-09-13 用户定稿）。

    调用点：正常收尾、录音意外中断、服务启动时的遗留清理——
    任何路径都不应在本地留下 pcm/wav/mp3。返回被删文件名列表。
    """
    removed = []
    if not task_dir or not os.path.isdir(task_dir):
        return removed
    for name in AUDIO_FILENAMES:
        p = os.path.join(task_dir, name)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(name)
            except Exception as e:
                print(f"[engine] 删除录音文件失败 {p}: {e}", flush=True)
    if removed and log:
        print(f"[engine] ✓ 已删除录音文件（不留语音）: {'、'.join(removed)}", flush=True)
    return removed


def finalize_archive(task_dir, minutes_text, meeting_date=None):
    """任务收尾（dsh 单目录机制 2026-08-27）：目录自始至终不变（YYYYMMDDNNN），
    不再 tmp→归档搬家。仅用 LLM 提取简短主题（4~12字）写回 metadata.json 的
    topic 字段，供历史任务列表显示。返回 (目录, 主题)。"""
    # 用 LLM 提取主题（供历史列表/交付显示；目录名已用序号，不再拼进目录）
    topic = ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        rec = MeetingRecorder.__new__(MeetingRecorder)  # 仅借 _chat，不触发 __init__
        rec._chat = MeetingRecorder._chat.__get__(rec, MeetingRecorder)
        topic = rec._chat(
            "你是会议主题提取助手。根据会议纪要提取一个简短主题（4~12字，不含日期），"
            "如'××公司研发会议'、'砂石贸易合作洽谈'。只输出主题本身，不要引号、不要多余文字。",
            f"会议纪要如下：\n\n{minutes_text[:6000]}",
            # 500 而非 50：deepseek 系推理模型，reasoning 会先吃预算，
            # 50 时 reasoning 就占满 → content 为空（2026-09-13 实测）
            max_tokens=500,
        ).strip().strip('"“”')
    except Exception as e:
        print(f"[engine] 主题提取失败: {e}，使用默认主题", flush=True)
    if not topic or len(topic) > 20:
        topic = "会议记录"
    safe_topic = "".join(c for c in topic if c not in '/\\:*?"<>|').strip()
    # 主题写回 metadata.json（历史任务列表显示用；失败不阻断）
    try:
        meta_path = os.path.join(task_dir, "metadata.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        meta["topic"] = safe_topic
        meta["archived_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return task_dir, safe_topic


class MeetingRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = "idle"          # idle / recording / paused / processing
        self.last_delivery = None    # 最近一次交付记录 {time, task_dir, has_minutes}
        self.last_error = ""
        self.task_dir = ""
        self.sentences = []          # [{ts, text}]
        self._start_ts = 0.0
        self._capture = None
        self._recognition = None
        self._push_thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()   # False=paused
        self._q = queue.Queue(maxsize=200)
        self._pcm_fh = None
        self._md_fh = None
        self._callback_done = threading.Event()
        self.files = {}              # {wav, transcript, minutes}
        self._asr_error = ""         # ASR 不可用时记录（录音仍继续）
        self._silent_error = ""      # 无声检测（设备无有效音频时提示）
        self._cloud_rel_offset = 0   # 云端连接重建后的相对秒偏移（暂停/恢复补偿）
        self._pause_ts = 0           # 暂停时刻（恢复偏移补偿）
        self._partial_text = ""      # 实时增量文本（qwen-audio 逐字输出，前端流式展示）
        self._last_switch_ts = 0     # 切换标记去重（5秒内不重复插标记）
        # ── 实时总结（录音中周期性 LLM 提炼）──
        self.summaries = []          # 已废弃（实时总结取消）
        # ── 会议材料（任意时刻提交，旁路不影响主流程）──
        self.materials = []          # [{path,name,text,ok}]
        self._materials_lock = threading.Lock()
        # ── 会议信息（主题/参会/地点，任意时刻可填，可选不阻塞）──
        self.meeting_info = {"title": "", "attendees": "", "location": "",
                             "host": "", "secretary": "", "extra": ""}
        self._info_lock = threading.Lock()
        # ── 补充信息（旁白/叮嘱：对话框式逐句追加，纪要生成时注入）──
        self.notes = []            # [{ts:"HH:MM:SS", text}]
        self._notes_lock = threading.Lock()
        self._last_minutes = None  # 纪要结构自检重试耗尽时的兜底结果
        # 2026-09-13 修复：asr_mode 原先只在 start() 里赋值，空闲时 state() 回落到硬编码
        # "local"，于是 /api/state 报告"本地识别"——与 start() 实际默认（auto=云端优先，
        # 即阿里百炼 qwen-audio）矛盾，容易让人误以为语音识别没走阿里。
        self.asr_mode = "auto"     # auto=云端优先（百炼 qwen-audio），云端不可用才降级本地
        # ── 后台任务（2026-09-13）：把"收尾+纪要生成"这类分钟级操作挪出 HTTP 请求 ──
        # 详见 start_job()；进度/结果通过 /api/state 的 job 字段暴露
        self._job = None
        self._job_lock = threading.Lock()
        self._audio_secs = 0       # 真实音频时长快照（PCM 被删后仍可读）
        # ── dsh 吸收（2026-08-27）──
        self._recent_sentences = deque(maxlen=5)  # 完整句去重滑动窗口（2-gram 重叠>0.6 丢弃）
        self._prev_input_device = ""              # 录音前系统默认输入设备（结束后恢复）
        self._cloud_send_fails = 0                # 云端 ASR 发送失败连续计数（≥3 显式降级提示）
        self._speech_fallback_used = False        # 本次任务是否用过 Speech 文件兜底转写

    # ── 会议信息：任意时刻填写/更新（可选，不阻塞主流程）──
    def set_info(self, title=None, attendees=None, location=None, host=None, secretary=None):
        """启动前/中/后任意时刻更新会议信息。返回 (ok, 合并后的信息)。"""
        with self._info_lock:
            if title is not None:
                self.meeting_info["title"] = title.strip()
            if attendees is not None:
                self.meeting_info["attendees"] = attendees.strip()
            if location is not None:
                self.meeting_info["location"] = location.strip()
            if host is not None:
                self.meeting_info["host"] = host.strip()
            if secretary is not None:
                self.meeting_info["secretary"] = secretary.strip()
            return True, dict(self.meeting_info)

    def get_info(self):
        """线程安全读取会议信息"""
        with self._info_lock:
            return dict(self.meeting_info)

    # ── 补充信息（旁白/叮嘱：对话框式逐句追加，落盘任务目录，纪要生成时注入）──
    def add_note(self, text):
        """追加一条补充信息（可随时提交，录音前/中/后均可）。返回 (ok, notes列表)。"""
        text = (text or "").strip()
        if not text:
            return False, self.get_notes()
        with self._notes_lock:
            self.notes.append({"ts": time.strftime("%H:%M:%S"), "text": text})
            self._persist_notes_locked()
            return True, list(self.notes)

    def delete_note(self, index):
        """删除一条补充信息（按序号，前端可回看后删错）。返回 (ok, notes列表)。"""
        with self._notes_lock:
            if 0 <= index < len(self.notes):
                self.notes.pop(index)
                self._persist_notes_locked()
                return True, list(self.notes)
            return False, list(self.notes)

    def update_note(self, index, text):
        """修改一条补充信息（用户补充可能有误，需可修正）。返回 (ok, notes列表)。"""
        text = (text or "").strip()
        if not text:
            return False, self.get_notes()
        with self._notes_lock:
            if 0 <= index < len(self.notes):
                self.notes[index]["text"] = text
                self._persist_notes_locked()
                return True, list(self.notes)
            return False, list(self.notes)

    def get_notes(self):
        """线程安全读取补充信息列表"""
        with self._notes_lock:
            return list(self.notes)

    def _persist_notes_locked(self):
        """补充信息落盘：任务目录/补充信息.md（带时间戳），会后 regen 也读它"""
        try:
            if not self.task_dir:
                return
            p = os.path.join(self.task_dir, "补充信息.md")
            with open(p, "w", encoding="utf-8") as f:
                for n in self.notes:
                    f.write(f"[{n['ts']}] {n['text']}\n")
        except Exception as e:
            print(f"[engine] 补充信息落盘失败: {e}", flush=True)

    def _restore_notes_from_disk(self):
        """断点续传：从任务目录 补充信息.md 恢复已提交的补充信息"""
        try:
            if not self.task_dir:
                return
            p = os.path.join(self.task_dir, "补充信息.md")
            if not os.path.exists(p):
                return
            self.notes = []
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("[") and "]" in line:
                        ts, _, text = line[1:].partition("]")
                        text = text.strip()
                        if text:
                            self.notes.append({"ts": ts.strip(), "text": text})
            if self.notes:
                print(f"[engine] 断点续传：恢复补充信息 {len(self.notes)} 条", flush=True)
        except Exception as e:
            print(f"[engine] 补充信息恢复失败: {e}", flush=True)

    # ── 会议材料：任意时刻提交（旁路操作，永不阻塞/影响主流程）──
    def add_materials(self, paths):
        """录音前/中/后任意时刻提交材料。后台线程解析，立即返回，不阻塞当前状态。
        返回立即信息；解析完成通过日志呈现。"""
        if not paths:
            return 0, ""
        paths = [p for p in paths if p]
        # 去重：已提交过的路径跳过
        with self._materials_lock:
            existing = {m.get("path") for m in self.materials}
        new_paths = [p for p in paths if p not in existing]
        if not new_paths:
            return 0, "材料已存在，无需重复提交"
        try:
            from materials import parse_materials, expand_paths
        except Exception as e:
            return 0, f"材料模块加载失败: {e}"

        # 目录场景：先展开，让返回信息准确（"目录内扫描出 N 个文件"）
        expanded = expand_paths(new_paths)
        real_n = len(expanded)

        def _load():
            try:
                parsed = parse_materials(new_paths)
                with self._materials_lock:
                    self.materials.extend(parsed)
                ok_n = sum(1 for m in parsed if m.get("ok"))
                print(f"[engine] 追加材料解析完成: {ok_n}/{real_n} 份成功", flush=True)
            except Exception as e:
                print(f"[engine] 材料解析失败(不阻断主流程): {e}", flush=True)
        threading.Thread(target=_load, daemon=True).start()
        if real_n != len(new_paths):
            return len(new_paths), f"已提交 {len(new_paths)} 个路径（扫描出 {real_n} 个文件），后台解析中"
        return len(new_paths), f"已提交 {len(new_paths)} 份材料，后台解析中"

    def get_materials(self):
        """线程安全读取材料列表"""
        with self._materials_lock:
            return list(self.materials)

    # ── 状态查询 ─────────────────────────────────────────────
    # ── 后台任务（2026-09-13 新增）──────────────────────────────
    def start_job(self, kind, fn):
        """把耗时操作（收尾+纪要、重生成纪要）丢到**后台线程**，立即返回。

        动机：`/api/stop` 原来要等整套"校对→核验→纪要→归档"跑完才响应，
        长会议实测可达 7+ 分钟——HTTP 请求一直挂着，前端只能干等。
        现在改为：请求立即返回 `state=processing`，进度与结果通过
        `GET /api/state` 的 `job` 字段读取（前端本来就每 2 秒轮询一次）。

        返回 (ok, info)：成功启动 → (True, job dict)；已有任务在跑 → (False, 原因)。
        """
        with self._job_lock:
            if self._job and self._job.get("running"):
                return False, f"已有后台任务在跑（{self._job.get('kind')}）"
            job = {"kind": kind, "running": True,
                   "started_at": time.time(), "finished_at": None,
                   "ok": None, "error": "", "result": None}
            self._job = job

        def _run():
            try:
                out = fn()
                # 约定 fn 返回 (ok, msg, files)；宽容处理别的形状
                if isinstance(out, tuple) and len(out) >= 3:
                    ok, msg, files = out[0], out[1], out[2]
                    job["ok"] = bool(ok)
                    job["error"] = "" if ok else str(msg)
                    job["result"] = {"task_dir": msg, "files": files}
                else:
                    job["ok"] = True
                    job["result"] = out
            except Exception as e:
                job["ok"] = False
                job["error"] = str(e)
                print(f"[engine] 后台任务 {kind} 失败: {e}", flush=True)
            finally:
                job["running"] = False
                job["finished_at"] = time.time()
                print(f"[engine] 后台任务 {kind} 结束（ok={job['ok']}）", flush=True)

        threading.Thread(target=_run, daemon=True, name=f"job-{kind}").start()
        return True, job

    def job_info(self):
        """给 /api/state 用：后台任务快照（无任务时 None）。"""
        with self._job_lock:
            if not self._job:
                return None
            j = dict(self._job)
        j["result"] = None if not j.get("result") else {
            "task_dir": j["result"].get("task_dir") if isinstance(j["result"], dict) else None}
        return j

    def mark_processing(self):
        """同步把状态从"正在录音"切到 processing（异步收尾用）。

        只接受 recording / paused：
        - 请求一返回就切状态，避免前端轮询出现"已提交却还显示录音中"的闪回；
        - **已经 processing 说明有流程在跑，必须拒绝**——否则会重复触发整套
          "校对→核验→纪要→归档"（2026-09-13 实测踩到：source 模式推流完会自动
          stop，此时手动再 POST /api/stop 又跑了一遍，日志里步骤3/步骤4 各出现两次）。
        """
        with self._lock:
            if self.state in ("recording", "paused"):
                self.state = "processing"
                return True
        return False

    def audio_seconds(self):
        """**真实音频时长**（秒）——由已捕获的 PCM 字节数推算，而不是墙上时钟。

        ⚠️ 2026-09-13 修复"前端显示录音时长=墙上跨度"：原实现用
        `time.time() - self._start_ts`，那算的是"从开始按按钮到现在"的墙上时间，
        包含了 ASR 断连后的空转、纪要生成耗时等。实测出现过"显示 621 分钟、
        实际音频只有 206 分钟"的偏差（ffprobe 验证），误导判断文件大小。

        采集格式固定 16kHz / 单声道 / int16 → 每秒 32000 字节。
        ⚠️ PCM 会在纪要成功后按"录音不保留"策略删除——所以这里**边采边存快照**
        （`_audio_secs`），文件没了就用最后快照，绝不能回落墙上时钟（否则时长会一直涨）。
        """
        pcm = (self.files or {}).get("pcm") or ""
        try:
            n = os.path.getsize(pcm)
        except Exception:
            n = 0
        if n > 0:
            self._audio_secs = int(n / (SAMPLE_RATE * 2))   # 采集中持续刷新
            return self._audio_secs
        if self._audio_secs:
            return self._audio_secs                          # PCM 已删 → 用快照（不再变化）
        if self._start_ts and self.state in ("recording", "paused"):
            return int(time.time() - self._start_ts)         # 刚起录、还没落盘
        return 0

    def status(self):
        dur = self.audio_seconds() if self._start_ts else 0
        return {
            "state": self.state,
            "task_dir": self.task_dir,
            "sentence_count": len(self.sentences),
            "duration_sec": dur,
            "last_error": self.last_error,
            "asr_error": self._asr_error,
            "silent_error": getattr(self, "_silent_error", ""),
            "asr_mode": getattr(self, "asr_mode", "auto"),
            "speech_fallback_used": getattr(self, "_speech_fallback_used", False),
            "files": self.files,
            "job": self.job_info(),      # 后台任务进度/结果（stop / regen_minutes 用）
            "summaries": list(self.summaries),
            "materials_count": len(self.get_materials()),
            "materials_ok": sum(1 for m in self.get_materials() if m.get("ok")),
            "meeting_info": self.get_info(),
            "last_delivery": self.last_delivery,
        }

    def sentence_list(self):
        return list(self.sentences)

    # ── 控制：开始 ───────────────────────────────────────────
    def start(self, device="", source=None, asr_mode="auto", materials=None, continue_dir=None):
        """source=None → sox 麦克风(默认输入设备，SwitchAudioSource 可切换)；source=文件路径 → 转写已有音频
        asr_mode: auto=**云端优先**（百炼 qwen-audio，云端不可用才降级本地 Speech）、
                  local=强制 macOS Speech（本地兜底）、cloud=强制百炼云端
        ⚠️ 语音识别只用**阿里百炼**（qwen-audio 主 / macOS Speech 兜底）——**DeepSeek 不参与识别**，
           DeepSeek（deepseek-flash）只做语义整理（纪要/主题）。
        materials: 可选，会议材料路径列表（会前准备阶段，并行解析供纪要参考）
        continue_dir: 可选，断点续传——复用 tmp 下出错任务目录，追加录音/流水（不丢已转写内容）"""
        with self._lock:
            if self.state not in ("idle",):
                return False, f"当前状态 {self.state}，不能开始"
            self.state = "recording"
            self.last_error = ""
        # 断点续传：复用已存在任务目录（YYYYMMDDNNN，兼容旧 tmp 路径），否则新建
        if continue_dir:
            name = os.path.basename(continue_dir)
            cd = os.path.join(MEETING_ROOT, name)
            if not os.path.isdir(cd):
                # 兼容旧版 tmp 路径（历史残留）
                cd = os.path.join(MEETING_ROOT, "tmp", name)
            self.task_dir = cd if os.path.isdir(cd) else make_task_dir()
            os.makedirs(self.task_dir, exist_ok=True)
        else:
            self.task_dir = make_task_dir()
        # 续传时保留已有流水（追加模式），否则全新
        self.sentences = []
        self.files = {}
        if continue_dir and os.path.isdir(self.task_dir):
            # 恢复已有流水句子
            flow_path = os.path.join(self.task_dir, "会议记录-流水.md")
            if os.path.exists(flow_path):
                import re as _re
                for l in open(flow_path, encoding="utf-8").read().splitlines():
                    m = _re.match(r"^(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})\s+(.*)$", l.strip())
                    if m:
                        self.sentences.append({"ts": m.group(1), "rel_s": 0,
                                               "text": m.group(3), "speaker": ""})
                print(f"[engine] 断点续传：恢复已有流水 {len(self.sentences)} 句", flush=True)
            # 已有 pcm → 追加模式
            pcm_existing = os.path.join(self.task_dir, "会议录音.pcm")
            if os.path.exists(pcm_existing):
                self.files["pcm_append"] = pcm_existing
                print("[engine] 断点续传：录音将追加到已有 pcm", flush=True)
        self.summaries = []
        self.materials = []        # 解析后的会议材料 [{path,name,text,ok}]
        self.meeting_info = {"title": "", "attendees": "", "location": "",
                             "host": "", "secretary": "", "extra": ""}
        # 补充信息：续传时从任务目录恢复，否则全新
        self.notes = []
        if continue_dir and os.path.isdir(self.task_dir):
            self._restore_notes_from_disk()
        self._start_ts = time.time()
        self._stop_event.clear()
        self._pause_event.set()          # 初始 = 非暂停
        self._callback_done.clear()
        self.asr_mode = asr_mode
        self.device = device if not source else ""
        # ── dsh 吸收（2026-08-27）：记录录音前系统默认输入设备，结束后恢复 ──
        self._prev_input_device = ""
        if device and not source:
            self._prev_input_device = self._get_input_device()
        self._cloud_send_fails = 0
        self._speech_fallback_used = False
        self.last_delivery = None   # 新任务开始，清掉上次交付
        self._task_log(f"任务开始 asr_mode={asr_mode}")
        self._asr_error = ""
        self._swift_proc = None
        self._swift_reader = None
        self._cloud_frames = 0     # 云端模式已喂帧计数（相对秒兜底）
        self._cloud_rel_offset = 0   # 新任务重置偏移

        # 会前准备阶段：并行解析会议材料（后台线程，不阻塞录音启动）
        if materials:
            self.add_materials(materials)

        # ── ASR 引擎选择（qwen-audio 优先，Speech 兜底）─────────
        # auto=先试云端(最准) → 失败降级本地Speech(免费)；cloud=强制云端；local=强制本地
        asr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr")
        swift_bin = os.path.join(asr_dir, "swift_asr_bin")
        swift_script = os.path.join(asr_dir, "swift_asr.swift")
        # 本地是否可用
        local_available = (os.path.exists(swift_bin) or os.path.exists(swift_script))
        # 引擎选择：auto 默认云端优先；local 强制本地；cloud 强制云端
        if asr_mode == "local":
            use_cloud = False
        elif asr_mode == "cloud":
            use_cloud = True
        else:  # auto
            use_cloud = True
        self._using_local = False

        if use_cloud:
            # 云端 qwen-audio-3.0-asr-flash-streaming（主引擎，新一代最准）
            try:
                self._init_cloud_asr()
                # _init_cloud_asr 内部已打印实际模型
            except Exception as e:
                # 云端不可用（欠费/网络/连接失败）→ 自动降级本地 Speech（绝不用 paraformer）
                print(f"[engine] 云端 ASR 不可用（{e}），降级本地 Speech", flush=True)
                self._recognition = None
                self._asr_error = f"云端ASR不可用，已降级本地Speech"
                if not local_available:
                    self._asr_error = f"识别服务不可用（{e}）——录音仍会保存，之后可用文件模式补转写"
                    print(f"[engine] {self._asr_error}", flush=True)
                else:
                    use_cloud = False

        if not use_cloud:
            # 本地 Speech 框架：子进程 stdin 喂 PCM，stdout 收 JSON 行
            # 优先预编译二进制（毫秒级启动），否则 swift 源码（需 JIT 编译）
            self._using_local = True
            if os.path.exists(swift_bin):
                swift_cmd = [swift_bin]
            else:
                swift_cmd = ["swift", swift_script]
            # stderr 落日志文件便于排障（之前 DEVNULL 导致问题不可见）
            asr_log = os.path.join(self.task_dir, "asr_stderr.log")
            try:
                self._swift_proc = subprocess.Popen(
                    swift_cmd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=open(asr_log, "w"))
                self._recognition = None
                # 等 READY 握手（Speech 识别器初始化完成）
                ready = False
                if self._swift_proc.stdout:
                    line = self._swift_proc.stdout.readline()
                    if line and b"READY" in line:
                        ready = True
                if not ready:
                    raise RuntimeError("Swift ASR 未就绪（未收到 READY）")
                print("[engine] ASR: 本地 Speech 框架", flush=True)
            except Exception as e:
                self._using_local = False
                self._asr_error = f"本地 ASR 启动失败（{e}）"
                print(f"[engine] {self._asr_error}", flush=True)
                if self._swift_proc:
                    try:
                        self._swift_proc.kill()
                    except Exception:
                        pass

        # 录音采集：source=文件 → ffmpeg 解码；source=None → sox 麦克风（默认输入设备）
        if source:
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", source,
                   "-af", VOLUME_FILTER,
                   "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
        else:
            # sox 采集默认输入设备（SwitchAudioSource 已切到目标设备）→ s16le 16k mono stdout
            self._set_input_device(device)
            cmd = ["sox", "-q", "-d", "-r", str(SAMPLE_RATE), "-c", "1",
                   "-e", "signed-integer", "-b", "16", "-t", "raw", "-"] + SOX_GAIN
        try:
            self._capture = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            if self._recognition is not None:
                try:
                    if hasattr(self._recognition, "finish"):
                        self._recognition.finish()
                    if hasattr(self._recognition, "close"):
                        self._recognition.close()
                except Exception:
                    pass
            with self._lock:
                self.state = "idle"
            return False, "未找到 sox/ffmpeg"

        pcm_path = os.path.join(self.task_dir, "会议录音.pcm")
        md_path = os.path.join(self.task_dir, "会议记录-流水.md")
        # 断点续传：pcm 追加模式（保留已有录音）
        append_pcm = continue_dir and os.path.exists(pcm_path)
        self._pcm_fh = open(pcm_path, "ab" if append_pcm else "wb")
        append_md = os.path.exists(md_path) and continue_dir and os.path.isdir(self.task_dir)
        self._md_fh = open(md_path, "a" if append_md else "w", encoding="utf-8")
        if not append_md:
            self._md_fh.write(f"# 会议全程记录（流水）\n\n")
        self._md_fh.flush()
        self.files = {"pcm": pcm_path, "transcript": md_path}

        # sox 启动存活检查（防 Input/output error 启动即死 → 立即报错，不假死）
        try:
            time.sleep(0.5)
            if self._capture and self._capture.poll() is not None:
                with self._lock:
                    self.state = "idle"
                self.last_error = (f"⚠ 录音设备启动失败：{self.device}（麦克风被占用或不可用，"
                                   f"请关闭占用设备的应用后重试）")
                print(f"[engine] {self.last_error}", flush=True)
                self._task_log("设备启动失败: " + self.last_error)
                # 启动即失败也要收干净（2026-09-13：录音一律不留）
                try:
                    if self._pcm_fh:
                        self._pcm_fh.close()
                        self._pcm_fh = None
                    if self._md_fh:
                        self._md_fh.close()
                        self._md_fh = None
                except Exception:
                    pass
                discard_audio_files(self.task_dir)
                for _k in ("pcm", "wav", "mp3", "pcm_append"):
                    self.files.pop(_k, None)
                return False, self.last_error
        except Exception as e:
            print(f"[engine] sox 存活检查异常: {e}", flush=True)

        # 单线程循环：读 sox → 写 ASR + 写 pcm（跨线程写 Swift stdin 不可靠，
        # 实测 reader→queue→pusher 三线程模式 0 输出，单线程顺序写 32 条正常）
        source_throttle = source is not None

        def pump():
            swift_written = 0
            cloud_frames = 0
            # 设备无感切换：sox 输出 EOF（设备断开/故障）时自动用备用设备重启
            # 备用设备列表：当前设备之外的其他 CoreAudio 输入设备（SwitchAudioSource 列出）
            alt_devices = []
            if not source:
                for d in self._list_input_devices():
                    if d and d != device:
                        alt_devices.append(d)
            restart_attempts = 0
            self._silent_error = ""       # 无声检测：设备无有效音频时记录
            last_chunk_ts = time.time()   # 上次读到 chunk 的时间
            while not self._stop_event.is_set():
                try:
                    chunk = self._capture.stdout.read(CHUNK)
                except Exception:
                    break
                if not chunk:
                    # 设备断开/sox 退出 → 尝试自动切换备用设备（无感恢复录音）
                    if not source and alt_devices and not self._stop_event.is_set() \
                            and restart_attempts < len(alt_devices):
                        alt = alt_devices[restart_attempts]
                        restart_attempts += 1
                        print(f"[engine] ⚠ 录音设备断开，尝试切换至 {alt}", flush=True)
                        self._task_log(f"设备断开，切换至 {alt}")
                        try:
                            self._capture.kill()
                        except Exception:
                            pass
                        try:
                            self._capture = self._spawn_capture(alt)
                            if self._capture is None:
                                raise RuntimeError("sox spawn 失败")
                            # 标记流水（录音设备切换，5秒去重避免与手动标记重复）
                            if time.time() - self._last_switch_ts > 5:
                                self._append_marker(f"📱 录音设备切换至 {alt}")
                                self._last_switch_ts = time.time()
                            print(f"[engine] ✓ 已切换到 {alt}，录音恢复", flush=True)
                            self._task_log(f"已切换到 {alt}")
                            continue
                        except Exception as e:
                            print(f"[engine] 设备切换失败: {e}", flush=True)
                            self._task_log(f"设备切换失败: {e}")
                    break
                restart_attempts = 0
                last_chunk_ts = time.time()
                self._silent_error = ""   # 有数据 → 清无声标记
                if self._pause_event.is_set():   # 未暂停
                    if self._using_local:
                        if self._swift_proc and self._swift_proc.stdin:
                            try:
                                self._swift_proc.stdin.write(chunk)
                                self._swift_proc.stdin.flush()
                                swift_written += len(chunk)
                            except Exception as e:
                                print(f"[engine] Swift stdin 写入失败: {e}", flush=True)
                    elif self._recognition is not None:
                        try:
                            self._recognition.send_audio(chunk)
                            cloud_frames += 1
                            self._cloud_frames = cloud_frames
                            # ── dsh 吸收（2026-08-27）：云端发送失败连续检测 → 显式降级提示 ──
                            # ⚠️ qwen_asr.error 是方法（def error(): return self._error），须调用
                            _asr_err = getattr(self._recognition, "error", "")
                            if callable(_asr_err):
                                _asr_err = _asr_err()
                            if _asr_err:
                                self._cloud_send_fails += 1
                                if self._cloud_send_fails >= 3:
                                    print(f"[engine] 云端 ASR 发送失败（连续 3 次: "
                                          f"{self._recognition.error}），转写暂不可用，录音仍保存",
                                          flush=True)
                                    self._asr_error = "云端ASR发送失败，已中断转写（录音仍保存，可用文件模式补转写）"
                                    self._cloud_send_fails = 0
                                    # 实时中断云端：置 None，后续 chunk 不再发送
                                    try:
                                        self._recognition.close()
                                    except Exception:
                                        pass
                                    self._recognition = None
                            else:
                                self._cloud_send_fails = 0
                        except Exception:
                            pass
                    self._pcm_fh.write(chunk)
                if source_throttle:
                    time.sleep(0.10)   # source 模式限流（100ms/块=实时速率）
                # S1/S2: 无声检测——超过 8 秒无音频数据（设备静音/卡死/无权限）
                if time.time() - last_chunk_ts > 8:
                    msg = (f"⚠ 录音可能无声：{int(time.time()-last_chunk_ts)}秒无音频数据"
                           f"（设备 {self.device}，请检查麦克风权限或切回有效设备）")
                    if self._silent_error != msg:
                        self._silent_error = msg
                        print(f"[engine] {msg}", flush=True)
                        self._task_log("无声检测: " + msg)
            mode = "Swift" if self._using_local else "云端"
            print(f"[engine] pump 结束, 写 {mode} {swift_written or cloud_frames} chunks", flush=True)
            # 假死修复：非手动停止 + 非 source → pump 意外退出（sox 设备错误/中断）
            # 置 state=idle + last_error，前端状态灯变红提示（不再假装 recording）
            if not self._stop_event.is_set() and source is None:
                try:
                    self._handle_interrupted_recording()
                except Exception as e:
                    print(f"[engine] 中断状态置位失败: {e}", flush=True)
            # source 模式：文件推流完毕 → 自动完成（等同 stop）
            if source is not None and not self._stop_event.is_set():
                print("[engine] source 推流完毕，自动完成", flush=True)
                # ── dsh 吸收（2026-08-27）：云端转写无结果 → macOS Speech 文件兜底转写 ──
                # （qwen 欠费/断网/结果为空时，source 模式不再只留空流水）
                if (not self._using_local and self._recognition is not None
                        and not self.sentences and not self._stop_event.is_set()):
                    print("[engine] 云端转写无结果，尝试 macOS Speech 文件兜底…", flush=True)
                    try:
                        self._speech_file_fallback(source)
                    except Exception as e:
                        print(f"[engine] Speech 兜底失败: {e}", flush=True)
                # 2026-09-13：自动收尾也走 start_job —— 这样它会登记进 _job，
                # ① 前端能看到进度；② 期间用户再点"结束会议"会被拒绝（防重复跑整套流程）。
                try:
                    started, _info = self.start_job(
                        "stop", lambda: self.stop(title="", attendees=""))
                    if not started:
                        # 已有任务在跑（用户刚手动停过）→ 不再重复触发
                        print(f"[engine] 自动完成跳过：{_info}", flush=True)
                except Exception as e:
                    print(f"[engine] 自动完成失败: {e}", flush=True)

        t_p = threading.Thread(target=pump, daemon=True)
        t_p.start()
        self._push_thread = t_p
        # 本地模式：启动 Swift stdout 读取线程（句子聚合）
        if self._using_local:
            self._start_swift_reader()
        return True, self.task_dir

    # ── CoreAudio 设备操作（SwitchAudioSource，sox 用 -d 录默认输入设备）────
    @staticmethod
    def _list_input_devices():
        """列出系统输入设备名（SwitchAudioSource -a -t input）。iPhone 优先排序。"""
        try:
            r = subprocess.run(["SwitchAudioSource", "-a", "-t", "input"],
                               capture_output=True, text=True, timeout=10)
            names = [s.strip() for s in (r.stdout or "").splitlines() if s.strip()]
            return sorted(names, key=lambda n: 0 if "iPhone" in n else 1)
        except Exception:
            return []

    @staticmethod
    def _set_input_device(name):
        """切换系统默认输入设备（sox -d 只录默认输入）。空名 = 不切换。"""
        if not name:
            return
        try:
            subprocess.run(["SwitchAudioSource", "-s", name, "-t", "input"],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass

    @staticmethod
    def _get_input_device():
        """当前系统默认输入设备名。"""
        try:
            r = subprocess.run(["SwitchAudioSource", "-c", "-t", "input"],
                               capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    def _spawn_capture(self, device):
        """启动 sox 采集（默认输入设备 → s16le 16k mono 管道）。返回 subprocess 或 None。
        先 SwitchAudioSource 切到目标设备，再 `sox -d`（-d 只录系统默认输入）。"""
        try:
            self._set_input_device(device)
            cmd = ["sox", "-q", "-d", "-r", str(SAMPLE_RATE), "-c", "1",
                   "-e", "signed-integer", "-b", "16", "-t", "raw", "-"] + SOX_GAIN
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            return None

    def _restore_input_device(self):
        """恢复录音前的系统默认输入设备（录音结束**或中断**后都该恢复，避免切到 iPhone 后留坑）。
        只恢复"当前仍是录音时切过去的那台"的情况，不覆盖用户手动切的新设备。"""
        if not self._prev_input_device:
            return
        try:
            cur = self._get_input_device()
            if cur and cur != self._prev_input_device:
                self._set_input_device(self._prev_input_device)
                print(f"[engine] 已恢复系统默认输入设备 → {self._prev_input_device}", flush=True)
        except Exception:
            pass
        self._prev_input_device = ""

    def _handle_interrupted_recording(self):
        """录音**意外中断**时的收尾（sox 异常退出 / 备用设备也全挂）——不叫 stop()，
        所以这里要自己做三件事，否则会留下"假装还在录"的状态和残留音频：
          1. state → idle，last_error 记原因（前端状态灯变红）
          2. 删掉本任务的 pcm/wav/mp3（"录音一律不留"，2026-09-13 用户定稿）
             —— 代价：该未收口任务不能再 `/api/continue` 续录旧音频（流水照常保留）
          3. 恢复录音前的系统默认输入设备
        """
        with self._lock:
            if self.state in ("recording", "paused"):
                self.state = "idle"
        self.last_error = (self._silent_error or
                           f"⚠ 录音中断：设备 {self.device} 异常退出（请检查麦克风占用/权限）")
        print(f"[engine] {self.last_error}", flush=True)
        self._task_log("录音中断: " + self.last_error)
        try:
            if self._pcm_fh:
                self._pcm_fh.close()
                self._pcm_fh = None
        except Exception:
            pass
        removed = discard_audio_files(self.task_dir)
        for k in ("pcm", "wav", "mp3", "pcm_append"):
            self.files.pop(k, None)
        if removed:
            self._task_log("录音中断，已删除语音文件（不留语音）: " + "、".join(removed))
        self._restore_input_device()

    def switch_device(self, device: str):
        """会议中手动切换录音设备（无感：pcm/ASR 连接不中断，仅重启 sox 采集源）。
        返回 (ok, msg)。"""
        if self.state not in ("recording", "paused"):
            return False, f"当前状态 {self.state}，不能切换设备"
        if not device:
            return False, "设备为空"
        try:
            # 杀旧 sox → 用新设备重启（_spawn_capture 内部 SwitchAudioSource 切默认输入）
            if self._capture:
                try:
                    self._capture.kill()
                except Exception:
                    pass
            new_proc = self._spawn_capture(device)
            if new_proc is None:
                # 恢复旧设备（杀失败了就尽力）
                return False, f"启动新设备 {device} 失败"
            self._capture = new_proc
            self.device = device
            if time.time() - self._last_switch_ts > 5:
                self._append_marker(f"🎤 录音设备切换至 {device}")
                self._last_switch_ts = time.time()
            self._task_log(f"手动切换设备 → {device}")
            print(f"[engine] ✓ 手动切换设备 → {device}", flush=True)
            return True, f"已切换至 {device}"
        except Exception as e:
            return False, f"设备切换失败: {e}"

    # ── 云端 ASR（qwen-audio-3.0-asr-flash-streaming，新一代）────────
    def _init_cloud_asr(self):
        """初始化云端识别：qwen-audio-3.0-asr-flash-streaming（原生 WebSocket run-task）。
        支持上下文增强（字典词+会议信息注入，显著提升专名准确率）。"""
        from qwen_asr import QwenASRClient, build_context_from_vocab
        recorder = self

        # 上下文增强：字典词 + 会议信息（参会人/主题/地点）
        try:
            ctx = build_context_from_vocab(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "vocab.json"))
        except Exception:
            ctx = ""
        info = self.get_info()
        extra_parts = []
        if info.get("title"):
            extra_parts.append(f"会议主题：{info['title']}")
        if info.get("attendees"):
            extra_parts.append(f"参会人员：{info['attendees']}")
        if info.get("location"):
            extra_parts.append(f"会议地点：{info['location']}")
        if info.get("host"):
            extra_parts.append(f"会议主持：{info['host']}")
        if info.get("secretary"):
            extra_parts.append(f"会议秘书：{info['secretary']}")
        if info.get("extra"):
            extra_parts.append(f"补充信息：{info['extra'][:500]}")
        if extra_parts:
            ctx = "；".join(extra_parts) + "。" + ctx

        class QW(QwenASRClient):
            def on_sentence(self, text, start_ms, end_ms):
                rel_sec = (start_ms or 0) / 1000.0 + recorder._cloud_rel_offset
                recorder._append_sentence(rel_sec, text, "")
                recorder._partial_text = ""   # 句子完成 → 清空增量

            def on_partial(self, text):
                # 增量完整文本 → 存 _partial_text（前端流式展示"正在说"）
                # 跨流去重：只接受比当前更长的文本（多 result 流重叠时防重复）
                if len(text) >= len(recorder._partial_text):
                    recorder._partial_text = text[:500]

        self._recognition = QW(
            api_key=load_api_key(),
            context=ctx,
            hotwords=_build_hotwords(),   # 字典全量热词（person/partner/project/company）
            model=ASR_MODEL,
        )
        ok = self._recognition.start()
        if not ok:
            raise RuntimeError(f"qwen-asr 连接失败: {self._recognition.error}")
        self._callback_done.clear()
        print(f"[engine] ASR: 云端 {ASR_MODEL}（上下文 {len(ctx)} 字）", flush=True)

    # ── macOS Speech 文件兜底转写（dsh 吸收 2026-08-27）────────────
    def _speech_file_fallback(self, source):
        """已有音频 → macOS Speech 文件批处理兜底转写（SFSpeechURLRecognitionRequest）。
        场景：source 模式 qwen 欠费/断网/结果为空 → 本地离线兜底，不占麦克风权限。
        数据源文件：asr/asr_macos.swift + asr/Info.plist + asr/asr_macos_bin（dsh 蒸馏）。"""
        asr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr")
        bin_path = os.path.join(asr_dir, "asr_macos_bin")
        swift_src = os.path.join(asr_dir, "asr_macos.swift")
        info_plist = os.path.join(asr_dir, "Info.plist")
        # 未编译（或源码更新）→ 自动编译。
        # ⚠️ 不嵌 Info.plist / 不调 requestAuthorization（2026-08-27 Hermes 适配）：
        #   显式授权会触发 TCC usage-description 检查，responsible process=Hermes.app
        #   无 NSSpeechRecognitionUsageDescription → abort。对齐 swift_asr.swift 隐式授权。
        if not os.path.exists(bin_path):
            r = subprocess.run(["swiftc", "-O", "-swift-version", "5",
                                swift_src, "-o", bin_path],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(bin_path):
                self._asr_error = f"Speech 文件兜底不可用（编译失败）: {(r.stderr or '')[-300:]}"
                print(f"[engine] {self._asr_error}", flush=True)
                return
        # 任意音频 → 16k mono wav（asr_macos_bin 需要带容器，裸 pcm 无头需显式格式）
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            if str(source).lower().endswith(".pcm"):
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le",
                                    "-ar", "16000", "-ac", "1", "-i", source,
                                    "-ar", "16000", "-ac", "1", tmp.name],
                                   capture_output=True, text=True, timeout=300)
            else:
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", source,
                                    "-ar", "16000", "-ac", "1", tmp.name],
                                   capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                self._asr_error = "Speech 兜底: 音频转 wav 失败"
                print(f"[engine] {self._asr_error}", flush=True)
                return
            rr = subprocess.run([bin_path, tmp.name, "--locale", "zh-CN"],
                                capture_output=True, text=True, timeout=1800)
            if rr.returncode != 0:
                err = (rr.stderr or "").strip()[-300:]
                if rr.returncode == 3 or "No speech" in err or "未检测到" in err:
                    self._asr_error = "Speech 兜底: 音频中未检测到语音"
                else:
                    self._asr_error = f"Speech 兜底失败: {err}"
                print(f"[engine] {self._asr_error}", flush=True)
                return
            # 解析 JSON 行 → 追加句子（ts 为音频相对秒，_append_sentence 自行换算墙上时钟）
            n = 0
            for ln in rr.stdout.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                txt = (obj.get("text") or "").strip()
                if not txt:
                    continue
                ts = int(obj.get("ts") or 0)
                self._append_sentence(ts, txt, "")
                n += 1
            if n:
                self._speech_fallback_used = True
                self._asr_error = ""
                print(f"[engine] ✓ Speech 文件兜底转写完成：{n} 句", flush=True)
            else:
                self._asr_error = "Speech 兜底: 识别结果为空（音频可能无声）"
                print(f"[engine] {self._asr_error}", flush=True)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    # ── 本地 ASR：Swift 进程读取线程（句子聚合）────────────
    def _start_swift_reader(self):
        """读取 Swift ASR stdout（JSON 行），聚合 partial 增量成句子"""

        def reader():
            proc = self._swift_proc
            if proc is None or proc.stdout is None:
                return
            pending = ""          # 当前聚合句
            last_text = ""        # 上次输出的完整文本
            last_update = time.time()
            pending_ts = 0.0      # 当前句对应的相对秒（Swift 输出的 ts）
            SENTENCE_GAP = 3.5    # 文本停止增长超时（秒）→ 落盘
            buf = ""

            def maybe_flush(force=False):
                nonlocal pending, last_text, last_update, pending_ts
                if pending and (force or (time.time() - last_update > SENTENCE_GAP)):
                    self._append_sentence(pending_ts, pending.strip())
                    pending = ""
                    last_text = ""
                    pending_ts = 0.0

            while not self._stop_event.is_set():
                try:
                    line = proc.stdout.readline()
                except Exception:
                    break
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                text = (obj.get("text") or "").strip()
                if not text:
                    continue
                # 相对秒（Swift 输出 relSec 数字；兼容旧字符串格式）
                raw_ts = obj.get("ts", 0)
                try:
                    rel_ts = float(raw_ts)
                except Exception:
                    rel_ts = 0.0
                # 增量逻辑：partial 完整文本与上次比较
                if text != last_text:
                    # 文本增长 → 更新当前句（取最长的版本）
                    if len(text) >= len(last_text):
                        pending = text
                        last_text = text
                        last_update = time.time()
                        pending_ts = rel_ts   # 用段起始相对秒
                    else:
                        # 文本回退（识别修正）：更新基线但不覆盖已更长的 pending
                        last_text = text
                        if len(pending) < len(text):
                            pending = text
                            pending_ts = rel_ts
                    # 超时即落盘当前句
                    maybe_flush()
            # 进程结束：落盘剩余
            maybe_flush(force=True)

        self._swift_reader = threading.Thread(target=reader, daemon=True)
        self._swift_reader.start()

    # ── 外部转写稿导入（腾讯会议/外部纪要 → 正式纪要）────────
    def import_transcript(self, text: str, title="", attendees="", location="",
                          materials=None, extra="", source_name="外部转写稿"):
        """导入外部转写稿（腾讯会议等），走完整链路：建任务→写流水→L2校对→实体核验→纪要→归档。
        extra: 人工补充信息（纪要要求/自定义内容），注入纪要提示词供 LLM 理解使用。
        返回 (task_dir, files)。"""
        with self._lock:
            if self.state != "idle":
                return False, f"当前状态 {self.state}，不能导入", {}
            self.state = "processing"
        text = (text or "").strip()
        if len(text) < 20:
            with self._lock:
                self.state = "idle"
            return False, "转写稿内容过少（<20字）", {}
        # 建任务目录
        self.task_dir = make_task_dir()
        self.sentences = []
        self.files = {}
        self.summaries = []
        # 材料：请求显式传 materials 则用新的；否则继承单例已提交的材料（如前端先提交材料再重写纪要）
        if materials:
            self.materials = []
        else:
            self.materials = self.get_materials()
        self.meeting_info = {"title": title, "attendees": attendees, "location": location}
        self._start_ts = time.time()
        self._stop_event.clear()
        self._pause_event.set()
        self._callback_done.clear()
        self._asr_error = ""
        self.asr_mode = "import"
        self._swift_proc = None
        self._swift_reader = None
        self._cloud_frames = 0
        # stop() 需要的其余属性（import 场景无录音/无 ASR，全部置空）
        self._using_local = False
        self._recognition = None
        self._push_thread = None
        self._capture = None
        self._pcm_fh = None
        self._md_fh = None
        self._entity_verified = {}
        self._entity_uncertain = []
        # 材料
        if materials:
            self.add_materials(materials)
        self._task_log(f"导入外部转写稿 {len(text)} 字符")
        # 转写稿 → 句子（按行切分，保留说话人前缀；无时间戳则用递增相对秒）
        import re as _re
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        self.sentences = []
        for i, l in enumerate(lines):
            # 先剥离行首时间戳前缀（[MM:SS-MM:SS] 或 [HH:MM:SS]），再解析 "XX：内容"
            body = l
            ts_prefix = ""
            m_ts = _re.match(r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?-\d{1,2}:\d{2}(?::\d{2})?\]?\s*", l)
            if m_ts:
                ts_prefix = m_ts.group(0)
                body = l[m_ts.end():]
            # 说话人前缀 "XX：内容"（XX 为 1-6 字中文/字母，后跟全角或半角冒号）
            m = _re.match(r"^([\u4e00-\u9fffA-Za-z]{1,6})[：:]\s*(.+)$", body)
            speaker = ""
            content = body
            if m:
                cand = m.group(1)
                # 仅当候选是常见说话人（2-4字中文，不含标点）才作 speaker
                if 2 <= len(cand) <= 4 and _re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cand):
                    speaker = cand
                    content = m.group(2)
            rel = i * 30
            h, mm, ss = rel // 3600, (rel % 3600) // 60, rel % 60
            self.sentences.append({"ts": f"{h:02d}:{mm:02d}:{ss:02d}", "rel_s": rel,
                                   "text": content, "speaker": speaker})
        # 写流水文件（真实递增时间戳，格式 HH:MM:SS-HH:MM:SS）
        md_path = os.path.join(self.task_dir, "会议记录-流水.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# 会议全程记录（流水）\n\n")
            for i, s in enumerate(self.sentences):
                start = s["rel_s"]
                end = start + 30
                sh, sm, ss = start // 3600, (start % 3600) // 60, start % 60
                eh, em, es = end // 3600, (end % 3600) // 60, end % 60
                spk = f"[{s['speaker']}] " if s["speaker"] else ""
                f.write(f"{sh:02d}:{sm:02d}:{ss:02d}-{eh:02d}:{em:02d}:{es:02d}  {spk}{s['text']}\n")
        self.files["transcript"] = md_path
        # 走 stop() 完整链路（校对→核验→纪要→归档→导出）
        # extra 补充信息存到 meeting_info，stop 生成纪要时注入提示词
        if extra and extra.strip():
            self.meeting_info["extra"] = extra.strip()
        ok, msg, files = self.stop(title=title, attendees=attendees, location=location)
        return ok, msg, files

    # ── 控制：暂停 / 恢复 ───────────────────────────────────
    def _append_marker(self, text):
        """在流水里插入标记行（如休息/恢复），也同步到 sentences 供前端展示"""
        now = datetime.datetime.now()
        clock = now.strftime("%H:%M:%S")
        self.sentences.append({"ts": clock, "rel_s": 0, "text": text,
                               "speaker": "", "marker": True})
        if self._md_fh:
            try:
                self._md_fh.write(f"\n## {text}（{clock}）\n")
                self._md_fh.flush()
            except Exception:
                pass

    def pause(self):
        with self._lock:
            if self.state != "recording":
                return False, f"当前状态 {self.state}，不能暂停"
            self.state = "paused"
        self._pause_event.clear()
        self._pause_ts = time.time()   # 记录暂停时刻（偏移补偿用）
        if self._capture:
            try:
                self._capture.send_signal(signal.SIGSTOP)
            except Exception:
                pass
        self._append_marker("⏸ 会议暂停")
        return True, ""

    def resume(self):
        with self._lock:
            if self.state != "paused":
                return False, f"当前状态 {self.state}，不能恢复"
            self.state = "recording"
        self._pause_event.set()
        if self._capture:
            try:
                self._capture.send_signal(signal.SIGCONT)
            except Exception:
                pass
        # 恢复：保持原 ASR 连接（qwen-audio 支持继续喂音频），
        # 仅当连接确实已关闭（callback_done）才重建——避免重放旧内容+时间错乱
        if not self._using_local and self._recognition is not None:
            try:
                if self._callback_done.is_set():
                    # 重建前先关旧连接（防泄漏），记录偏移：当前已识别最大 rel_s + 本次暂停时长
                    try:
                        if hasattr(self._recognition, "close"):
                            self._recognition.close()
                    except Exception:
                        pass
                    max_rel = max((s.get("rel_s", 0) for s in self.sentences), default=0)
                    pause_sec = time.time() - self._pause_ts if self._pause_ts else 0
                    self._cloud_rel_offset = max_rel + pause_sec
                    print(f"[engine] ASR 连接已关闭，恢复时重建（偏移 {self._cloud_rel_offset}s）", flush=True)
                    self._init_cloud_asr()
                    self._task_log("恢复时重建 ASR（原连接已关闭）")
            except Exception as e:
                print(f"[engine] ASR 重建失败: {e}", flush=True)
        self._append_marker("▶ 会议恢复")
        return True, ""

    # ── 控制：结束（五步流程）──────────────────────────────
    # 1) 终止录音，清理后台 → 2) 检查流水完整保存 → 3) 基于流水生成正式纪要
    # → 4) 创建交付目录(日期+主题)并移动产物 → 5) 返回交付信息（通知用户）
    def stop(self, title="", attendees="", location=""):
        with self._lock:
            # import_transcript 场景已置 processing（由外部转写稿直接走收口）
            if self.state not in ("recording", "paused", "processing"):
                return False, f"当前状态 {self.state}，没有进行中的录音", {}
            if self.state == "processing":
                # 防重入：已在处理中（可能 pump 自动 stop + 用户手动 stop 竞争）
                # 若 task_dir 已被归档（目录不存在）→ 直接返回已有结果
                if self.task_dir and not os.path.isdir(self.task_dir):
                    return True, self.task_dir, self.files
            self.state = "processing"

        # 合并会议信息：请求传的优先，未传用之前 set_info 存的值
        info = self.get_info()
        if not title:
            title = info.get("title", "")
        if not attendees:
            attendees = info.get("attendees", "")
        if not location:
            location = info.get("location", "")
        host = info.get("host", "")
        secretary = info.get("secretary", "")

        # ══════════════════════════════════════════════════
        # 步骤1：终止录音，清理后台
        # ══════════════════════════════════════════════════
        print("[engine] 步骤1：终止录音，清理后台...", flush=True)
        self._stop_event.set()
        try:
            if self._capture:
                self._capture.terminate()
        except Exception:
            pass
        if self._push_thread and self._push_thread is not threading.current_thread():
            self._push_thread.join(timeout=5)

        # 等识别 flush 尾部句子（云端模式）
        if not self._using_local and self._recognition is not None:
            try:
                if hasattr(self._recognition, "finish"):
                    self._recognition.finish()
                if hasattr(self._recognition, "close"):
                    self._recognition.close()
            except Exception as e:
                self.last_error = f"stop: {e}"
            self._callback_done.wait(timeout=8)

        # 本地模式：关闭 Swift stdin → EOF → 触发尾句输出，稍等读取线程
        if self._using_local and self._swift_proc:
            try:
                if self._swift_proc.stdin:
                    self._swift_proc.stdin.close()
            except Exception:
                pass
            try:
                self._swift_proc.wait(timeout=6)
            except Exception:
                try:
                    self._swift_proc.kill()
                except Exception:
                    pass

        if self._pcm_fh:
            self._pcm_fh.close()
        if self._md_fh:
            self._md_fh.close()

        # ── dsh 吸收（2026-08-27）：录音结束后恢复系统默认输入设备（避免切 iPhone 后留坑）──
        self._restore_input_device()

        # ══════════════════════════════════════════════════
        # 步骤2：检查会议流水记录被完整保存
        # ══════════════════════════════════════════════════
        md_path = self.files.get("transcript", "")
        flow_ok = False
        if md_path and os.path.exists(md_path):
            with open(md_path, encoding="utf-8") as f:
                flow_text = f.read()
            # 校验：流水文件数据行数应 >= 内存句子数
            # （数据行 = 以 "HH:MM:SS-HH:MM:SS" 时间段开头的行）
            import re as _re
            data_lines = [l for l in flow_text.splitlines()
                          if _re.match(r"^\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}", l.strip())]
            # 内存真实转写句数（排除 marker 暂停/恢复标记句）
            real_sent_count = len([s for s in self.sentences if not s.get("marker")])
            if real_sent_count > 0 and len(data_lines) >= real_sent_count:
                flow_ok = True
            else:
                self.last_error = (f"⚠ 流水保存不完整（文件 {len(data_lines)} 行 vs 内存 {real_sent_count} 句）")
                print(f"[engine] {self.last_error}", flush=True)
        if flow_ok:
            print(f"[engine] ✓ 流水完整保存（{len(data_lines)} 句，{len(flow_text)} 字）", flush=True)
        else:
            if not self.last_error:
                self.last_error = "⚠ 流水保存不完整（流水文件缺失）"
                print(f"[engine] {self.last_error}", flush=True)

        # 流水 md 收尾（统计信息）
        if md_path and os.path.exists(md_path):
            with open(md_path, "a", encoding="utf-8") as f:
                f.write(f"\n---\n录音时长: {self.audio_seconds()} 秒\n"
                        f"句子数: {len(self.sentences)}\n")
            self.files["transcript"] = md_path

        # pcm → wav（完整原始录音产物）
        pcm_path = self.files.get("pcm", "")
        wav_path = os.path.join(self.task_dir, "会议录音.wav")
        if pcm_path and os.path.exists(pcm_path) and os.path.getsize(pcm_path) > 44:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                 "-i", pcm_path, wav_path], timeout=60)
        self.files["wav"] = wav_path if os.path.exists(wav_path) else ""

        # ══════════════════════════════════════════════════
        # 步骤2.5：转录校对（L2 三级纠错体系）
        # ══════════════════════════════════════════════════
        proof_files = {}
        clean_flow_text = ""
        if md_path and os.path.exists(md_path):
            try:
                from proofread import proofread_flow
                with open(md_path, encoding="utf-8") as f:
                    flow_lines = f.read().splitlines()
                pr = proofread_flow(flow_lines)
                clean_flow_text = pr["clean_text"]
                # 清洗稿
                clean_path = os.path.join(self.task_dir, "会议记录-清洗稿.md")
                with open(clean_path, "w", encoding="utf-8") as f:
                    f.write(f"# 会议流水（清洗稿，L2 校对后）\n\n{clean_flow_text}\n")
                proof_files["clean"] = clean_path
                # 纠错对照清单
                diff_path = os.path.join(self.task_dir, "纠错对照清单.md")
                with open(diff_path, "w", encoding="utf-8") as f:
                    f.write("# 纠错对照清单\n\n| 原词 | 改为 | 归因 | 处置 |\n|---|---|---|---|\n")
                    for c in pr["changes"]:
                        f.write(f"| {c['原词']} | {c['正词']} | {c['归因']} | {c['处置']} |\n")
                    f.write(f"\n共 {pr['change_count']} 处。\n")
                proof_files["diff"] = diff_path
                # 存疑清单
                if pr["uncertain"]:
                    unc_path = os.path.join(self.task_dir, "存疑清单.md")
                    with open(unc_path, "w", encoding="utf-8") as f:
                        f.write("# 存疑清单（需人工核对）\n\n")
                        for u in pr["uncertain"]:
                            f.write(f"- [{u['原词']}] {u['正词']}（{u['归因']}）\n")
                    proof_files["uncertain"] = unc_path
                self.files.update(proof_files)
                print(f"[engine] ✓ L2 校对完成: {pr['change_count']} 处修正, "
                      f"{len(pr['uncertain'])} 处待核", flush=True)
            except Exception as e:
                print(f"[engine] L2 校对跳过: {e}", flush=True)
        self._clean_flow = clean_flow_text   # 纪要生成用它

        # ══════════════════════════════════════════════════
        # 步骤2.7：实体核验（J 三级匹配：别名表→台账→roster）
        # ══════════════════════════════════════════════════
        self._entity_verified = {}     # {token: 规范词}（确证项，纪要静默采用）
        self._entity_uncertain = []    # [{token, evidence, candidates}]（待人工）
        try:
            from verify_entities import verify_text, apply_verified
            ev = verify_text(clean_flow_text or "".join(
                f"[{s['ts']}] {s['text']}" for s in self.sentences))
            if ev["verified"]:
                self._entity_verified = ev["verified"]
                # 确证项应用到清洗稿（静默改，纪要不留痕迹）
                clean_flow_text = apply_verified(clean_flow_text, ev["verified"])
                self._clean_flow = clean_flow_text
            if ev["uncertain"]:
                self._entity_uncertain = ev["uncertain"]
                unc_path = os.path.join(self.task_dir, "实体核验-待确认.md")
                with open(unc_path, "w", encoding="utf-8") as f:
                    f.write("# 实体核验·待确认清单（需人工补充）\n\n")
                    for u in ev["uncertain"]:
                        f.write(f"- {u['token']}：{u['evidence']}"
                                + (f"（候选：{'、'.join(u['candidates'][:3])}）" if u["candidates"] else "")
                                + "\n")
                self.files["verify"] = unc_path
            print(f"[engine] ✓ 实体核验: 确证 {len(ev['verified'])} 项, "
                  f"待确认 {len(ev['uncertain'])} 项", flush=True)
        except Exception as e:
            print(f"[engine] 实体核验跳过: {e}", flush=True)

        # ══════════════════════════════════════════════════
        # 步骤3：基于完整流水 → LLM 提炼正式版会议纪要
        # ══════════════════════════════════════════════════
        self._task_log("步骤3：生成纪要")
        print("[engine] 步骤3：基于流水生成正式版纪要...", flush=True)
        # 环境变量 MEETING_SKIP_MINUTES=1 时跳过纪要（用户只看流水/测试用）
        minutes_path = os.path.join(self.task_dir, "会议纪要.md")
        # 健壮性：task_dir 可能因异常被移走/删除 → 确保目录存在
        if self.task_dir and not os.path.isdir(self.task_dir):
            os.makedirs(self.task_dir, exist_ok=True)
            print(f"[engine] ⚠ task_dir 不存在，已重建: {self.task_dir}", flush=True)
        if os.environ.get("MEETING_SKIP_MINUTES") == "1":
            print("[engine] 跳过纪要生成（MEETING_SKIP_MINUTES=1）", flush=True)
        else:
            try:
                minutes = self._make_minutes(title=title, attendees=attendees,
                                             location=location, host=host,
                                             secretary=secretary)
                with open(minutes_path, "w", encoding="utf-8") as f:
                    f.write(minutes)
                self.files["minutes"] = minutes_path
                print(f"[engine] ✓ 会议纪要已生成（{len(minutes)} 字）", flush=True)
                # 结构化元数据输出（minutes 设计吸收：metadata.json 便于检索/二次处理）
                try:
                    meta_path = os.path.join(self.task_dir, "metadata.json")
                    meta = {
                        "title": title or "",
                        "attendees": attendees or "",
                        "location": location or "",
                        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        "sentence_count": len(self.sentences),
                        "duration_sec": self.audio_seconds(),
                        "asr_mode": getattr(self, "asr_mode", ""),
                        "minutes_chars": len(minutes),
                        "materials_count": len(self.get_materials()),
                        "proofread_changes": len(getattr(self, "_clean_flow", "") or "") > 0,
                    }
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                    self.files["metadata"] = meta_path
                except Exception as e:
                    print(f"[engine] metadata 生成失败(不阻断): {e}", flush=True)
                # 导出到 Obsidian 收件箱 + 知识库 meetings/（失败不阻断主流程；2026-09-11 由 reports/ 修正）
                try:
                    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    from export_minutes import export_all
                    ex = export_all(minutes, title, task_dir=os.path.basename(self.task_dir))
                    self.files["obsidian"] = ex["obsidian"]
                    self.files["wiki"] = ex["wiki"]
                    print(f"[engine] 已导出 Obsidian/WIKI", flush=True)
                except Exception as e:
                    print(f"[engine] 导出失败(不阻断): {e}", flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.last_error = f"纪要生成失败: {e}"
                print(f"[engine] {self.last_error}", flush=True)
                minutes_path = ""

        # ══════════════════════════════════════════════════
        # 步骤3.5：材料归档（会前/会中提交的材料复制进任务目录 materials/）
        # ══════════════════════════════════════════════════
        try:
            ok_mats = [m for m in self.get_materials() if m.get("ok")]
            if ok_mats:
                mat_dir = os.path.join(self.task_dir, "materials")
                os.makedirs(mat_dir, exist_ok=True)
                for m in ok_mats:
                    src = m.get("path", "")
                    if src and os.path.exists(src):
                        dst = os.path.join(mat_dir, os.path.basename(src))
                        if not os.path.exists(dst):
                            import shutil
                            shutil.copy2(src, dst)
                self.files["materials_dir"] = mat_dir
                print(f"[engine] ✓ 材料已归档: {len(ok_mats)} 份 → materials/", flush=True)
        except Exception as e:
            print(f"[engine] 材料归档失败(不阻断): {e}", flush=True)

        # ══════════════════════════════════════════════════
        # 步骤4：创建交付目录（日期+主题），产物从 tmp 移入
        # ══════════════════════════════════════════════════
        self._task_log("步骤4：归档")
        print("[engine] 步骤4：创建交付目录并归档...", flush=True)
        try:
            if minutes_path and os.path.exists(minutes_path):
                with open(minutes_path, encoding="utf-8") as f:
                    minutes_text = f.read()
            else:
                # 无纪要时用流水内容提主题（前 2000 字）
                minutes_text = ""
                _md = self.files.get("transcript", "")
                if _md and os.path.exists(_md):
                    minutes_text = open(_md, encoding="utf-8").read()[:2000]
            archived_dir, topic = finalize_archive(self.task_dir, minutes_text)
            print(f"[engine] ✓ 交付目录: {archived_dir} (主题: {topic})", flush=True)
            self.task_dir = archived_dir
            # 更新 files 里的相对路径
            for k, v in list(self.files.items()):
                if v and os.path.dirname(v) != archived_dir:
                    base = os.path.basename(v)
                    newp = os.path.join(archived_dir, base)
                    if os.path.exists(newp):
                        self.files[k] = newp
            # ── 录音一律不留（2026-09-13 用户定稿，比 09-09 版更彻底）──
            # 09-09 版：纪要失败/跳过时生成 mp3 兜底保留，供人工复核或 source 补转写。
            # 09-13 用户明确"不要在本地保留录音的语音文件"，故**取消 mp3 兜底**：
            # 无论纪要成功与否，收尾时都删掉 pcm/wav/mp3（见下方统一清理）。
            # 代价与兜底：纪要失败时靠**流水文件**（步骤2 已可靠落盘）重生成，
            # 走 `/api/regen_minutes` 即可，不需要音频；只有"转写本身为空"这种
            # 极端情况才真的无从恢复，此时会打显著警告（见下）。
        except Exception as e:
            print(f"[engine] 归档异常(不阻断): {e}", flush=True)

        # ── 录音一律不留（2026-09-13 用户定稿；此前只在纪要成功时才删）──
        # 无条件删除 pcm/wav/mp3：纪要成不成功、有没有跳过，都不在本地留语音文件。
        # 纪要失败时的补救走流水文件 + `/api/regen_minutes`（不需要音频）。
        try:
            removed = []
            for _name in ("会议录音.pcm", "会议录音.wav", "会议录音.mp3"):
                _ap = os.path.join(self.task_dir, _name)
                if os.path.exists(_ap):
                    os.remove(_ap)
                    removed.append(_name)
            # tmp/旧目录位置也扫一遍，避免归档后残留
            for _d in (archived_dir, os.path.join(MEETING_ROOT, "tmp")):
                if not _d or not os.path.isdir(_d):
                    continue
                for _name in ("会议录音.pcm", "会议录音.wav", "会议录音.mp3"):
                    _ap = os.path.join(_d, _name)
                    if os.path.exists(_ap):
                        try:
                            os.remove(_ap)
                            removed.append(os.path.join(os.path.basename(_d), _name))
                        except Exception:
                            pass
            for _k in ("pcm", "wav", "mp3", "pcm_append"):
                self.files.pop(_k, None)
            if removed:
                print(f"[engine] ✓ 已删除录音文件（不留语音）: {'、'.join(removed)}", flush=True)
        except Exception as e:
            print(f"[engine] 录音文件清理失败: {e}", flush=True)

        # 显著警告：既没有纪要、流水也是空的 → 这场会议没有可恢复的文本产物了
        if not (minutes_path and os.path.exists(minutes_path)) and not self.sentences:
            self.last_error = (self.last_error + "；" if self.last_error else "") + \
                "无纪要且无流水（录音已按不留语音策略删除，无法再补转写）"
            print("[engine] ⚠⚠ 警告：本次既无纪要也无流水，且录音已删除——这场会议没有可恢复的文本产物",
                  flush=True)

        with self._lock:
            self.state = "idle"
        # 产物已交付记录（前端状态指示：暗绿+呼吸灯提醒查看）
        self.last_delivery = {
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task_dir": self.task_dir,
            "has_minutes": bool(self.files.get("minutes")),
        }
        return True, self.task_dir, self.files

    # ── LLM 纪要（严格遵循 references/会议纪要质量标准.md）──
    # 模板固化（2026-08-31）：唯一权威源 = skill 的 references/会议纪要质量标准.md。
    # engine 每次生成实时读取该文件注入 system prompt——改模板文件即改所有后续纪要，
    # 不再与代码内硬编码副本漂移。文件缺失时回退内置精简版（不阻断）。
    # 模板正主 = 服务目录/references（共享服务自持，2026-09-09 起）
    MINUTES_STANDARD_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "references", "会议纪要质量标准.md")

    def _load_minutes_standard(self):
        try:
            p = self.MINUTES_STANDARD_PATH
            if os.path.exists(p):
                txt = open(p, encoding="utf-8").read().strip()
                if txt:
                    return txt
        except Exception as e:
            print(f"[engine] 读取纪要质量标准失败（回退内置）: {e}", flush=True)
        return ""

    def _validate_minutes_structure(self, minutes, is_mapreduce=False):
        """生成后结构自检（模板固化校验）：返回 (ok, 缺失项列表)。
        校验四大件：标题三要素 / 六要素信息区 / 开篇总概括 / 编号正文段落。
        任一缺失 → 上层自动重试，避免输出残缺纪要。"""
        missing = []
        if not minutes or len(minutes.strip()) < 100:
            return False, ["内容过短"]
        lines = [l.strip() for l in minutes.splitlines() if l.strip()]
        if not lines:
            return False, ["空输出"]
        # 1. 标题：YYYY年M月D日<主体><会议性质>纪要（首行）
        if not re.match(r"^\d{4}年\d{1,2}月\d{1,2}日.*纪要$", lines[0]):
            missing.append("标题三要素")
        # 2. 六要素信息区（会议主题/时间/地点/参会人员/主持/秘书）
        keys = ["会议主题", "会议时间", "会议地点", "参会人员", "会议主持", "会议秘书"]
        found = sum(1 for k in keys
                    if any(l.startswith(k + "：") or l.startswith(k + ":") for l in lines[:15]))
        if found < 4:
            missing.append(f"会议信息({found}/6)")
        # 3. 开篇总概括（含"纪要如下"或"会议"开头段落）
        if not any("纪要如下" in l or "纪要如下：" in l for l in lines[:20]):
            missing.append("开篇总概括")
        # 4. 正文编号段落（一、二、三…）
        if not any(re.match(r"^[一二三四五六七八九十]+、", l) for l in lines):
            missing.append("正文编号段落")
        return (len(missing) == 0), missing

    def _make_minutes(self, title="", attendees="", location="", host="", secretary="",
                      retries=2, model=LLM_MODEL):
        # 优先用 L2 清洗稿（已纠错），否则用原始流水
        clean = getattr(self, "_clean_flow", "") or ""
        if clean.strip():
            transcript = clean
        else:
            transcript = "\n".join(f"[{s['ts']}] {s['text']}" for s in self.sentences)
        # 说话人自动命名（MeetMemo 设计吸收）：流水含"说话人N"时，结合人名职责索引映射真实人名
        if "说话人" in transcript:
            try:
                transcript = self._identify_speakers(transcript, model=model)
            except Exception as e:
                print(f"[engine] 说话人命名失败(不阻断): {e}", flush=True)
        if len(transcript) < 20:
            return "# 会议纪要\n\n（转写内容过少，无法生成有效纪要）"
        # ── 模板固化（2026-08-31）：唯一权威源 = references/会议纪要质量标准.md ──
        # 实时读取模板全文注入 system prompt；文件缺失才用内置精简版兜底（不阻断）。
        std = self._load_minutes_standard()
        if std:
            system = (
                "你是专业的会议纪要整理助手。你会收到一份会议转写文本（带时间段）及会议基本信息。\n"
                "请严格按照下方《正式版会议纪要质量标准》输出正式版会议纪要。"
                "质量标准的每一项都必须执行：标题三要素、六行会议信息、开篇总概括、编号正文段落、附件。\n"
                "格式要求：纯文本，不用【】方括号标记，不用 markdown 的 # 标题符号，直接输出完整纪要文本。\n\n"
                "==========《正式版会议纪要质量标准》（唯一模板，逐条执行，不得省略任何部分）==========\n"
                f"{std}\n"
                "==========质量标准结束==========\n"
            )
        else:
            system = (
                "你是专业的会议纪要整理助手，严格遵循《正式版会议纪要质量标准》。"
                "你会收到一份会议转写文本（带时间段）及会议基本信息。"
                "请按以下格式输出正式版会议纪要——纯文本，不用【】方括号标记，不用 markdown 的 # 标题符号：\n\n"
                "第一行写纪要标题（格式 YYYY年M月D日<主体><会议性质>纪要，如'2022年9月11日××项目股东会第一次会议纪要'）。"
                "标题三要素：时间（哪天开的会，用系统日期）+ 主体（跟什么事有关）+ 会议性质（股东会/经营工作会/周例会等），对全文分析提炼；"
                "会议内容未明示部门或性质时按讨论内容判断（研发进度→'××公司研发部周例会'，经营目标→'××集团经营工作专项会议'）；无法判断时主体用主题、性质用'会议'。\n\n"
                "接着空一行，写六行会议信息（顺序固定，key: value 格式，不要加任何标题标记）：\n"
                "会议主题：<本次会议核心主题>\n"
                "会议时间：<YYYY年M月D日HH:MM-HH:MM，起止时间，用系统日期和时间，不得编造>\n"
                "会议地点：<具体地点，未提供则留空>\n"
                "参会人员：<姓名顿号分隔，有现场/连线之分时分别标注；未提供则从内容推断>\n"
                "会议主持：<召集人或主持人，取参会人员中职务最高者（如董事长高于总经理），无法确定则留空>\n"
                "会议秘书：<做纪要的人（起草者），无法确定则留空>\n\n"
                "接着空一行，写纪要正文：\n"
                "1. 开篇一段总概括：什么时间、为解决什么问题、哪些人、在什么地方召开会议，讨论了什么，形成共识/决议如下（结尾'纪要如下：'）。\n"
                "2. 一个主题一个段落，段落用'一、二、三…'编号，段内细则用'1.1、1.2…'或'（2.1.1）（2.1.2）…'层级编号。\n"
                "3. 决议性内容用'会议要求''会议明确''会议决定''会议授权''会议拟定'等固定动词开头。\n"
                "4. 言简意赅：每句有信息量，不堆砌客套话；忠实原文不编造、不遗漏重要决议。\n"
                "5. 文风（融入正文，不要单独列章节）：对外会议（政府/客户/合作方）突出客户/政府必须落实的要点和风险预警；对内会议（内部经营/研发/例会）补充各方态度和下一步行动计划。\n\n"
                "最后（可选），附件：会议内容提到材料（如'见附件二'）时列'附件：'清单；未提及时留空。\n\n"
                "缺失处理：会议信息/附件任何无法从内容或用户输入确定的要素一律留空，不得编造。\n"
                "直接输出完整纪要文本，不要额外解释。\n"
            )
        vp = vocab_prompt()
        if vp:
            system += (
                "\n\n【术语归一】以下是本单位的专属词汇字典（规范词=口语近似词/同音词/简称）。整理纪要时：\n"
                "1. 转写中的近似词、同音词、口语简称一律改写为规范词"
                "（例：口语近似写法 → 规范全称；错别字/同音写法 → 规范词）\n"
                "2. 只改写字典中明确列出的词，其他保持原样\n"
                "3. 人名：口语称呼能对应字典中唯一人名时写全名，否则保留原称呼\n"
                "4. 简称消歧：当'X总'对应多人时，按【简称消歧】规则根据会议讨论内容判断具体指谁；"
                "无法确定时保留原称呼，不得臆断\n"
                "5. 正式版会议纪要正文一律使用全名，不用简称\n\n"
                f"字典：\n{vp}"
            )
        info_lines = []
        if title:
            info_lines.append(f"会议主题: {title}")
        if location:
            info_lines.append(f"会议地点: {location}")
        if attendees:
            info_lines.append(f"参会人员: {attendees}")
        if host:
            info_lines.append(f"会议主持: {host}")
        if secretary:
            info_lines.append(f"会议秘书: {secretary}")
        # 系统日期时间（录音开始时刻，权威不猜）
        try:
            start_dt = datetime.datetime.fromtimestamp(self._start_ts) if self._start_ts else datetime.datetime.now()
        except Exception:
            start_dt = datetime.datetime.now()
        sys_date = start_dt.strftime("%Y年%m月%d日")
        sys_time = start_dt.strftime("%H:%M")
        sys_duration_min = max(1, int((time.time() - (self._start_ts or time.time())) / 60))
        # 结束时间 = 开始 + 录音时长（供纪要"起止时间 HH:MM-HH:MM"使用，不再只给开始时间）
        try:
            end_dt = start_dt + datetime.timedelta(minutes=sys_duration_min)
            sys_end_time = end_dt.strftime("%H:%M")
        except Exception:
            sys_end_time = ""
        info_lines.append(f"系统会议日期: {sys_date}")
        info_lines.append(f"系统开始时间: {sys_time}")
        if sys_end_time:
            info_lines.append(f"系统结束时间: {sys_end_time}")
        info_lines.append(f"系统录音时长: {sys_duration_min} 分钟")
        info_block = "\n".join(info_lines) if info_lines else "会议主题: （由内容概括）"
        body = (f"【会议基本信息】\n{info_block}\n\n"
                f"【会议转写文本】\n{transcript[:22000]}")
        # 补充信息注入（import 场景用户人工填写的纪要要求 + 会中对话框式旁白/叮嘱 notes）
        _extra = self.get_info().get("extra", "")
        _notes = self.get_notes()
        _user_notes_parts = []
        if _notes:
            _user_notes_parts.append(
                "\n".join(f"[{n['ts']}] {n['text']}" for n in _notes))
        if _extra:
            _user_notes_parts.append(_extra)
        if _user_notes_parts:
            body += f"\n\n【补充信息（用户提供，需理解并用于纪要编写）】\n" + \
                    "\n".join(_user_notes_parts)[:4000]
            system += ("\n\n【补充信息使用规则】用户补充的信息是明确的纪要要求/会议事实/纠错指示，"
                       "必须理解并纳入纪要（如补充的会议时间/地点/参会人/讨论要求/纠错后的专名/重点标记等）；"
                       "与转写冲突时以用户补充信息为准（用户更权威）。"
                       "带时间戳的补充信息表示用户在会议进行到该时刻时做的旁白，同样有效。")
        # 会前材料注入（防幻觉参考依据；材料与转写冲突时以转写为准，材料只做背景）
        mats = self.get_materials()
        ok_mats = [m for m in mats if m.get("ok")]
        if ok_mats:
            try:
                from materials import materials_summary
                mat_text = materials_summary(ok_mats)
                body += f"\n\n【会议材料（参考依据）】\n{mat_text[:16000]}"
                system += ("\n\n【材料使用规则】"
                           "会议材料（附件）仅作背景参考，帮助理解议题、核对专名/数字；"
                           "纪要事实以会议转写为准；材料与转写冲突时以转写为准并标注'（材料与会上表述不一致）'；"
                           "材料未提及的内容不得写入纪要。")
            except Exception:
                pass
        # 实体核验待确认项注入（J：未确证实体，纪要留空/原样，不臆断）
        uncertain = getattr(self, "_entity_uncertain", None) or []
        if uncertain:
            unc_text = "；".join(f"{u['token']}({u['evidence']})" for u in uncertain[:10])
            body += f"\n\n【未核验实体（不得臆断，留空或原样保留）】\n{unc_text}"
            system += ("\n\n【实体核验规则】上述'未核验实体'未经台账/名单确认，"
                       "纪要中涉及处留空待人工补充，或按转写原样保留，不得自行推断为其他名称。")

        # 长文 MapReduce（吸收 Scribble 设计）：转写超长时分段摘要→分层合并→最终合成
        CHUNK_CHARS = 15000
        OVERLAP_CHARS = 1000
        GROUP_SIZE = 4
        if len(transcript) > CHUNK_CHARS:
            try:
                print(f"[engine] 长文会议（{len(transcript)} 字），启用 MapReduce 分段生成", flush=True)
                return self._make_minutes_mapreduce(
                    system, body, transcript, title, attendees, location,
                    model=model, retries=retries,
                    chunk_chars=CHUNK_CHARS, overlap_chars=OVERLAP_CHARS, group_size=GROUP_SIZE)
            except Exception as e:
                print(f"[engine] MapReduce 失败，回退单次生成: {e}", flush=True)

        last_exc = None
        for attempt in range(retries):
            try:
                # max_tokens 增大到 16000（2026-08-26 修复）：deepseek 系是推理模型，
                # reasoning_content 偶发吃光 8000 token → content 空（finish=length）。
                # 16000 给 reasoning + content 都留足空间，根治纪要 0 字节。
                # 2026-08-31：_chat 内部已对 finish=length 截断自动放大重试；此处再做结构自检，
                # 残缺纪要（缺标题/信息区/开篇/编号段）不静默放行，重试补全。
                minutes = self._chat(system, body, max_tokens=16000, model=model)
                ok, missing = self._validate_minutes_structure(minutes)
                if ok:
                    return minutes
                self._last_minutes = minutes   # 记住最后一份（重试耗尽时兜底返回）
                print(f"[engine] 纪要结构自检未通过(第{attempt+1}次): {missing}，重试", flush=True)
                # 重试提示：让模型知道缺了什么，避免重复犯
                if attempt < retries - 1:
                    body += (f"\n\n【上次输出结构检查未通过，缺失: {'、'.join(missing)}】"
                             "请严格按质量标准补全上述缺失部分，输出完整纪要。")
            except Exception as e:
                last_exc = e
                print(f"[engine] 纪要 LLM 调用失败(第{attempt+1}次, {model}): {e}", flush=True)
                time.sleep(2)
        # 结构校验重试耗尽：若有生成结果则返回（宁可保留不完美纪要，不丢内容）；
        # 全是调用异常则抛最后一个异常。
        #
        # 2026-09-13 修复：原判断写的是 `if "minutes" in dir(self) and self._last_minutes`
        # ——`minutes` 是循环内的**局部变量**，而 `dir(self)` 返回的是**属性名**，
        # 所以这个条件恒为假，兜底分支从来没执行过：结构自检两次不过就直接抛
        # "LLM 调用异常且无有效输出"，整条纪要链路失败（实测复现，第二轮测试即栽在这里）。
        if getattr(self, "_last_minutes", None):
            print("[engine] 纪要结构自检重试耗尽，返回最后一份结果（可能不完整）", flush=True)
            return self._last_minutes
        if last_exc:
            raise last_exc
        raise RuntimeError("纪要生成失败：LLM 调用异常且无有效输出")

    # ── 长文 MapReduce（Scribble 设计吸收）─────────────────
    def _make_minutes_mapreduce(self, system, body, transcript, title, attendees, location,
                                model=LLM_MODEL, retries=2, chunk_chars=15000,
                                overlap_chars=1000, group_size=4):
        """长会议流水：分段 → 每段摘要 → 每N段合并 → 最终合成完整纪要"""
        # 1. 分段（按行，带重叠）
        lines = transcript.split("\n")
        chunks = []
        cur, cur_len = [], 0
        for line in lines:
            cur.append(line)
            cur_len += len(line) + 1
            if cur_len >= chunk_chars:
                chunks.append("\n".join(cur))
                # 重叠尾部
                overlap, ov_len = [], 0
                for i in range(len(cur) - 1, -1, -1):
                    ov_len += len(cur[i]) + 1
                    if ov_len >= overlap_chars:
                        break
                    overlap.insert(0, cur[i])
                cur, cur_len = overlap, ov_len
        if cur:
            chunks.append("\n".join(cur))

        # 2. 每段独立摘要（保留关键信息，不丢决议/数字/人名）
        seg_prompt = (
            "你是会议纪要分段整理助手。以下是长会议的一个片段（带时间段）。"
            "请提炼本段的关键信息，保留：讨论要点、决议、数字/金额/日期、人名、待办事项。"
            "输出结构化要点（分条），不要客套，不要省略重要内容。"
        )
        seg_summaries = []
        for i, chunk in enumerate(chunks, 1):
            for attempt in range(retries):
                try:
                    s = self._chat(seg_prompt, f"【片段{i}/{len(chunks)}】\n{chunk}", max_tokens=2500, model=model)
                    seg_summaries.append(s.strip())
                    break
                except Exception as e:
                    if attempt == retries - 1:
                        raise
                    time.sleep(2)

        # 3. 分层合并（每 group_size 段合并为一段）
        def merge_level(items):
            merged = []
            for i in range(0, len(items), group_size):
                group = items[i:i + group_size]
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                merge_prompt = (
                    "你是会议纪要合并助手。以下是同一会议的若干分段要点，请合并为一份连贯的要点稿：\n"
                    "- 保留所有不同主题；\n"
                    "- 保留决议、数字、人名、待办；\n"
                    "- 删除重复内容；\n"
                    "- 按主题逻辑排序输出。"
                )
                joined = "\n\n---\n\n".join(group)
                for attempt in range(retries):
                    try:
                        m = self._chat(merge_prompt, joined, max_tokens=3000, model=model)
                        merged.append(m.strip())
                        break
                    except Exception as e:
                        if attempt == retries - 1:
                            merged.append(joined)  # 合并失败保留原文
                            break
                        time.sleep(2)
            return merged

        level = seg_summaries
        while len(level) > 1:
            level = merge_level(level)

        # 4. 最终合成：分段要点稿 + 原 system 提示词 → 完整纪要
        final_body = body.replace(transcript[:22000], "【分段要点稿】\n" + level[0])
        # 若要点稿超长则截断（保留主要信息）
        if len(level[0]) > 30000:
            final_body = body.replace(transcript[:22000], "【分段要点稿】\n" + level[0][:30000])
        # 2026-08-31 固化：最终合成后结构自检（MapReduce 是本次 8-31 事故的截断点——
        # finish=length 静默返回半截纪要）。_chat 已内置截断放大重试，此处再补结构校验。
        last_minutes = None
        for attempt in range(retries):
            try:
                minutes = self._chat(system, final_body, max_tokens=16000, model=model)
                ok, missing = self._validate_minutes_structure(minutes, is_mapreduce=True)
                if ok:
                    return minutes
                last_minutes = minutes
                print(f"[engine] MapReduce 纪要结构自检未通过(第{attempt+1}次): {missing}，重试", flush=True)
                if attempt < retries - 1:
                    final_body += (f"\n\n【上次输出结构检查未通过，缺失: {'、'.join(missing)}】"
                                   "请严格按质量标准补全上述缺失部分，输出完整纪要。")
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep(2)
        if last_minutes:
            print("[engine] MapReduce 纪要结构自检重试耗尽，返回最后一份结果（可能不完整）", flush=True)
            return last_minutes
        raise RuntimeError("MapReduce 纪要生成失败：LLM 调用异常且无有效输出")

    # ── 多模板输出（Scribble 设计吸收：MoM/Summary/Bullets/DeepAnalysis）──
    def _make_by_template(self, template, title, attendees, location, model=LLM_MODEL, retries=2):
        """按模板生成：minutes(正式纪要)/summary(摘要)/bullets(要点)/analysis(深析)"""
        clean = getattr(self, "_clean_flow", "") or ""
        transcript = clean if clean.strip() else "\n".join(f"[{s['ts']}] {s['text']}" for s in self.sentences)
        if len(transcript) < 20:
            return "（转写内容过少）"
        info = []
        if title:
            info.append(f"会议主题: {title}")
        if attendees:
            info.append(f"参会人员: {attendees}")
        if location:
            info.append(f"会议地点: {location}")
        # 系统日期注入（模板场景同样不猜日期）
        try:
            start_dt = datetime.datetime.fromtimestamp(self._start_ts) if self._start_ts else datetime.datetime.now()
        except Exception:
            start_dt = datetime.datetime.now()
        info.append(f"系统会议日期: {start_dt.strftime('%Y年%m月%d日')}")
        info.append(f"系统开始时间: {start_dt.strftime('%H:%M')}")
        info_block = "\n".join(info) if info else "会议主题: （由内容概括）"
        body = f"【会议基本信息】\n{info_block}\n\n【会议转写文本】\n{transcript[:22000]}"
        mats = self.get_materials()
        ok_mats = [m for m in mats if m.get("ok")]
        if ok_mats:
            try:
                from materials import materials_summary
                body += f"\n\n【会议材料（参考依据）】\n{materials_summary(ok_mats)[:12000]}"
            except Exception:
                pass
        T = {
            "summary": (
                "你是专业内容摘要助手。请对会议转写做一份综合性摘要：\n"
                "1. 首行标题：# <主题>\n2. 2-3句总览\n3. 按逻辑顺序列要点（段落或分组）\n"
                "4. 保留说话人名字和职务（如可用）\n5. 决议/待办/关键信息高亮\n"
                "规则：不编造事实，缺失写'无数据'；输出 Markdown。"),
            "bullets": (
                "你是内容摘要助手。把会议转写提炼为要点清单：\n"
                "1. 首行：# <短标题>\n2. 用 - 列出要点（按主题分组）\n3. 每条1-2句\n"
                "4. 保留相关说话人\n5. 决议/待办单列为 ## Decisions / ## Action Items\n"
                "规则：不编造，最多30条；输出 Markdown。"),
            "analysis": (
                "你是资深会议分析师。对会议转写做逐段深度分析：\n"
                "按时间顺序分主题章节，每章包含：主要内容（[时间段] 说话人 — 发言要点）、"
                "发现的问题（谁提出、直接引用）、关键决定与技术要点表格。\n"
                "规则：不遗漏决议；每个行动项标注 What-Who-When；发言必须归属说话人；"
                "保留关键引用；结束时汇总所有行动项和未决问题。"),
        }
        system = T.get(template, T["summary"])
        last_exc = None
        for attempt in range(retries):
            try:
                return self._chat(system, body, max_tokens=16000, model=model)
            except Exception as e:
                last_exc = e
                time.sleep(2)
        raise last_exc

    # ── 周际待办联动（博维 weekly-action-linking 设计吸收）──
    def _make_weekly(self, title, attendees, location, prev_minutes_path=None,
                     model=LLM_MODEL, retries=2):
        """周期性例会：提取上一期待办 → 五态复盘进展 → 生成本周待办。
        prev_minutes_path: 上一期纪要/流水路径（无则走普通纪要）。"""
        clean = getattr(self, "_clean_flow", "") or ""
        transcript = clean if clean.strip() else "\n".join(f"[{s['ts']}] {s['text']}" for s in self.sentences)
        if len(transcript) < 20:
            return "# 会议纪要\n\n（转写内容过少）"
        info = []
        if title:
            info.append(f"会议主题: {title}")
        if attendees:
            info.append(f"参会人员: {attendees}")
        if location:
            info.append(f"会议地点: {location}")
        # 系统日期注入（周际场景同样不猜日期）
        try:
            start_dt = datetime.datetime.fromtimestamp(self._start_ts) if self._start_ts else datetime.datetime.now()
        except Exception:
            start_dt = datetime.datetime.now()
        info.append(f"系统会议日期: {start_dt.strftime('%Y年%m月%d日')}")
        info.append(f"系统开始时间: {start_dt.strftime('%H:%M')}")
        info_block = "\n".join(info) if info else "会议主题: （由内容概括）"
        # 上一期待办提取
        prev_actions = ""
        if prev_minutes_path and os.path.exists(prev_minutes_path):
            try:
                prev_text = open(prev_minutes_path, encoding="utf-8").read()[:8000]
                extract_prompt = (
                    "从上一期会议纪要中提取所有待办事项，格式：\n"
                    "编号 | 事项 | 负责人 | 原定时限\n"
                    "无待办则输出'无'。")
                prev_actions = self._chat(extract_prompt, prev_text, max_tokens=800, model=model)
            except Exception as e:
                prev_actions = f"（上一期待办提取失败: {e}）"

        system = (
            "你是周期性例会纪要助手。生成结构：\n"
            "# <标题> 会议纪要（YYYY年第XX周）\n"
            "一、会议基本信息（时间/地点/主持人/与会者）\n"
            "二、会议议程（3-4条，结论化）\n"
            "三、会议小结与决议（分主题，每条 action title 带数字）\n"
            "四、待办事项跟踪（最后实质段落）：\n"
            "  （一）上周待办事项进展——五态：已完成/进行中/未启动/阻塞/取消；\n"
            "  （二）本周待办事项——编号/事项/负责人/追踪人/完成时限/来源（承接上周-XX 或 本周新增）\n"
            "规则：\n"
            "1. 进展状态只使用五态，不得把'会上未提及'推断为'未完成'\n"
            "2. 已完成/取消保留在上周复盘表，不进本周待办；进行中/未启动/阻塞承接本周\n"
            "3. 行动项四要素齐全：内容/责任人/完成时限/交付物\n"
            "4. 言简意赅，忠实原文不编造，缺失留空待补"
        )
        body = (f"【会议基本信息】\n{info_block}\n\n"
                f"【上一期待办】\n{prev_actions if prev_actions else '无上一期数据（首次建档）'}\n\n"
                f"【本周会议转写】\n{transcript[:22000]}")
        mats = self.get_materials()
        ok_mats = [m for m in mats if m.get("ok")]
        if ok_mats:
            try:
                from materials import materials_summary
                body += f"\n\n【会议材料】\n{materials_summary(ok_mats)[:12000]}"
            except Exception:
                pass
        last_exc = None
        for attempt in range(retries):
            try:
                return self._chat(system, body, max_tokens=16000, model=model)
            except Exception as e:
                last_exc = e
                time.sleep(2)
        raise last_exc

    # ── 说话人自动命名（MeetMemo identify_speakers 设计吸收）──
    def _identify_speakers(self, transcript, model=LLM_MODEL):
        """LLM 结合人名职责索引，把'说话人N'映射为真实人名。
        仅当说话人标签与真实人名可明确对应时才替换；不确定的保留原标签。"""
        # 人名职责索引（消歧依据，可选）——优先本地 .local.md，其次仓库内模板
        idx_path = settings.speaker_index()
        idx_text = ""
        if idx_path and os.path.exists(idx_path):
            try:
                with open(idx_path, encoding="utf-8") as f:
                    idx_text = f.read()
            except Exception:
                pass
        prompt = (
            "你是会议说话人识别助手。会议转写中说话人标签为'说话人0/1/2…'。\n"
            "根据说话内容（发言内容、讨论主题、职务语境）和【人名职责索引】判断每个说话人可能是谁。\n"
            "规则：\n"
            "1. 能明确判断（发言主题与某人职责/项目高度吻合）→ 用全名替换标签\n"
            "2. 无法确定或多人可能 → 保留原标签'说话人N'\n"
            "3. 只替换标签，不改动其他文字\n"
            "输出：替换后的完整文本。"
        )
        if idx_text:
            prompt += f"\n\n【人名职责索引】\n{idx_text[:4000]}"
        return self._chat(prompt, transcript, max_tokens=6000, model=model)

    def _chat(self, system, user, max_tokens=8000, model=LLM_MODEL):
        import urllib.request
        # 按模型选端点：deepseek 系 → DeepSeek API（主账户）；其他 → 百炼专属域名
        if str(model).startswith("deepseek"):
            base = "https://api.deepseek.com/v1"
            key = load_ds_api_key()
        else:
            base = settings.dashscope_compatible_url()
            key = load_api_key()
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            base + "/chat/completions", data=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read().decode("utf-8"))
        content = d["choices"][0]["message"]["content"]
        finish = d["choices"][0].get("finish_reason")
        usage = d.get("usage", {})
        # ── 预算不足优先处理（2026-09-13 修复顺序 BUG）──
        # finish_reason=length 意味着 token 预算被耗尽。deepseek 系是**推理模型**，
        # reasoning_content 也吃 completion 预算：预算太小时会出现"reasoning 占满、
        # content 为空、finish=length"（实测 max_tokens=50 时 reasoning_tokens=50、content=''）。
        # 所以必须**先判断 length 再判空**——原来的顺序是先判空直接抛错，
        # 于是这种"预算不足"被误报成"模型无输出"，害得纪要整条链路失败。
        # 重试给一个合理下限（50→1000 这种翻倍太慢），上限 32000。
        if finish == "length" and max_tokens < 32000:
            nxt = min(max(max_tokens * 2, 1000), 32000)
            print(f"[engine] 输出被预算截断（finish=length, completion="
                  f"{usage.get('completion_tokens')}, content="
                  f"{'空' if not (content or '').strip() else '半截'}）"
                  f"，max_tokens {max_tokens}→{nxt} 重试", flush=True)
            return self._chat(system, user, max_tokens=nxt, model=model)
        if finish == "length":
            raise RuntimeError(
                f"模型输出仍被截断（finish=length, max_tokens={max_tokens}, "
                f"completion={usage.get('completion_tokens')}）")
        # 真·空输出（非预算原因）才抛错，交给上层 retry，避免纪要 0 字节
        if not content or not content.strip():
            raise RuntimeError(
                f"模型返回空 content（finish={finish}, "
                f"completion_tokens={usage.get('completion_tokens')}）")
        return content

    # P2-10: 任务级日志（崩溃可回溯）
    def _task_log(self, msg):
        try:
            log_path = os.path.join(self.task_dir, "task.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    def _append_sentence(self, ts, text, speaker=""):
        """ts: 相对会议开始的秒（float/int）。流水格式：'16:51:10-16:52:33 文字'"""
        # 归一：兼容 HH:MM:SS 旧格式和数字秒
        if isinstance(ts, str) and ":" in str(ts):
            try:
                parts = str(ts).split(":")
                if len(parts) == 3:
                    ts = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                elif len(parts) == 2:
                    ts = int(parts[0]) * 60 + int(parts[1])
            except Exception:
                ts = 0
        try:
            ts = float(ts)
        except Exception:
            ts = 0.0
        rel_s = int(ts)
        # 短句/语气词过滤（qwen 增量流噪声：え/啊/嗯/哦 等 ≤2 字且无实义）
        _t = (text or "").strip()
        if _t and len(_t) <= 2 and not any(ord(c) > 0x4E00 for c in _t):
            return  # 纯符号/语气词（非中文）跳过
        if _t and len(_t) <= 2 and _t in ("え", "啊", "嗯", "哦", "呃", "唉", "哎", "哈"):
            return
        # ── dsh 吸收（2026-08-27）：完整句去重（2-gram 重叠>0.6）+ 实时专名纠错（rt_correct）──
        _b_grams = [_t[i:i + 2] for i in range(len(_t) - 1)]
        if _b_grams:
            for _prev in self._recent_sentences:
                if not _prev:
                    continue
                _pa = set(_prev[i:i + 2] for i in range(len(_prev) - 1))
                if _pa and sum(1 for g in _b_grams if g in _pa) / len(_b_grams) > 0.6:
                    return  # qwen 长段重复输出（与最近句重叠 >60%）→ 丢弃
        self._recent_sentences.append(_t)
        # 实时专名纠错：命中 vocab 变体 → 替换为规范词（复用 proofread.load_dict/proofread_text）
        try:
            import proofread as _P
            _map = _P.load_dict()
            if _map:
                _r = _P.proofread_text(_t, mapping=_map)
                if isinstance(_r, dict) and _r.get("clean"):
                    _t = _r["clean"]
                    text = _t
        except Exception:
            pass
        # 时间段：录音开始时刻 + rel_s → 墙上时钟（"HH:MM:SS"）
        try:
            start_clock = datetime.datetime.fromtimestamp(self._start_ts)
            seg_start = start_clock + datetime.timedelta(seconds=rel_s)
            seg_end = seg_start + datetime.timedelta(seconds=10)
            time_range = f"{seg_start.strftime('%H:%M:%S')}-{seg_end.strftime('%H:%M:%S')}"
        except Exception:
            time_range = f"第{rel_s}秒"
        # 时间戳（墙上时钟，前端展示用）和相对秒（流水格式用）
        clock = datetime.datetime.now().strftime("%H:%M:%S")
        # P2-8: 内存句子上限（2万句 ≈ 5.5 小时会议；超限后只写流水文件，不占内存）
        MAX_SENTENCES = 20000
        if len(self.sentences) < MAX_SENTENCES:
            self.sentences.append({"ts": clock, "rel_s": rel_s, "text": text, "speaker": speaker})
        if self._md_fh:
            try:
                prefix = f"[{speaker}] " if speaker else ""
                # 流水格式：时间段 + 文字（如 "16:51:10-16:52:33  文字"）
                line = f"{time_range}  {prefix}{text}\n"
                self._md_fh.write(line)
                self._md_fh.flush()
            except Exception:
                pass


# 全局单例（插件后端进程内）
_recorder = None


def get_recorder():
    global _recorder
    if _recorder is None:
        _recorder = MeetingRecorder()
    return _recorder
