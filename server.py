"""meeting-server 独立后端服务（浏览器版会议记录员）

由备份插件 plugin_api.py 改造：从「Hermes 插件挂载」变为「独立 FastAPI 服务」，
同源 serve 前端页面（static/index.html）+ API，供任意 AI Agent / 浏览器访问。

运行：bash start.sh            （推荐；内部用 runtime.py 解析解释器）
      python3 server.py        （也行；缺依赖时会自动切到依赖齐全的解释器）
端口：8789
产物：<服务目录>/records/YYYYMMDDNNN/{会议记录-流水.md, 会议记录-清洗稿.md, 会议纪要.md, metadata.json}
      （收口后**不保留任何录音文件**：pcm/wav/mp3 一律删除，2026-09-13 用户定稿）
配置：全部走环境变量 / .env，权威清单见 settings.py 顶部 docstring（SKILL.md「运维要点 §配置」有摘要）
"""
import html as html_lib
import os
import re
import subprocess
import sys
import time
import datetime
from pathlib import Path

# serve 进程加载本文件时不保证本目录在 sys.path，先自行注册
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime import ensure_runtime, ensure_native_libs
ensure_runtime()          # 解释器缺依赖时自动切换到依赖齐全的环境（只重入一次）
ensure_native_libs()      # 让 WeasyPrint 找到 Homebrew 的 GLib/Pango（PDF 导出用）

import settings

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from engine import get_recorder, MeetingRecorder, MEETING_ROOT, LLM_MODEL, load_api_key

app = FastAPI(title="会议记录员", version="1.0.0")


# P1-7: 统一错误响应结构 {ok: false, detail}
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def _uniform_error(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "detail": str(exc.detail)},
    )


@app.exception_handler(Exception)
async def _uniform_500(request, exc):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": f"服务器内部错误: {exc}"},
    )


@app.on_event("startup")
def _startup_maintenance():
    """启动维护：清理旧 tmp 崩溃残留（兼容历史）+ 容量告警 + 中断任务检测（单目录机制 2026-08-27）"""
    # P0-2: 清理旧版 tmp 残留（历史遗留目录，>1 天）；新版单目录机制不再产生 tmp
    tmp_root = RECORD_DIR / "tmp"
    if tmp_root.is_dir():
        cutoff = time.time() - 86400
        cleaned = 0
        for d in tmp_root.iterdir():
            if d.is_dir():
                try:
                    mtime = d.stat().st_mtime
                    if mtime < cutoff:
                        import shutil
                        shutil.rmtree(d)
                        cleaned += 1
                except Exception:
                    pass
        if cleaned:
            print(f"[server] 启动清理: 删除 {cleaned} 个过期 tmp 残留", flush=True)

    # P0-3: 磁盘容量告警（>1GB 提示归档）
    try:
        total = sum(f.stat().st_size for f in RECORD_DIR.rglob("*")
                    if f.is_file())
        if total > 1024 ** 3:
            print(f"[server] ⚠ 会议存档 {total / 1024 ** 3:.1f}GB，建议归档清理", flush=True)
    except Exception:
        pass

    # P1-4: 检测中断任务（MEETING_ROOT 下今天有录音/流水但无 metadata 的目录 → 未正常收口）
    today = datetime.datetime.now().strftime("%Y%m%d")
    for d in RECORD_DIR.iterdir():
        if not d.is_dir() or d.name == "tmp" or not d.name.startswith(today):
            continue
        has_work = (d / "会议记录-流水.md").exists() or (d / "会议录音.pcm").exists()
        if has_work and not (d / "metadata.json").exists():
            print(f"[server] ⚠ 发现中断任务 {d.name}（有录音/流水但未收口），"
                  f"可调用 POST /api/recover 恢复", flush=True)
            break

    # P1-5: 遗留语音文件一律清理（2026-09-13 用户定稿："录音一律不留"）
    # 服务刚启动、不可能有正在进行的录音，此时还在的 会议录音.pcm/wav/mp3
    # 只可能是上次异常退出（录音被中断 / 进程被杀）留下的，按策略删掉。
    try:
        removed_files = 0
        freed_bytes = 0
        for d in RECORD_DIR.rglob("*"):
            if not d.is_dir():
                continue
            for name in ("会议录音.pcm", "会议录音.wav", "会议录音.mp3"):
                p = d / name
                if p.exists():
                    try:
                        freed_bytes += p.stat().st_size
                        p.unlink()
                        removed_files += 1
                    except Exception:
                        pass
        if removed_files:
            print(f"[server] 启动清理: 删除 {removed_files} 个遗留录音文件（中断录音不留语音），"
                  f"释放 {freed_bytes / 1024 ** 2:.1f}MB", flush=True)
    except Exception as e:
        print(f"[server] 遗留录音清理跳过: {e}", flush=True)

    # R1: 清理残留 sox/swift 子进程（上次异常退出遗留，防多进程抢占麦克风/端口）
    try:
        import subprocess as _sp
        cleaned_procs = 0
        # sox 麦克风采集进程（-q -d 录音特征，避免误杀用户手工 sox 转码）
        r = _sp.run(["pgrep", "-f", "sox -q -d"],
                    capture_output=True, text=True)
        for pid in r.stdout.split():
            try:
                _sp.run(["kill", "-9", pid], timeout=5)
                cleaned_procs += 1
            except Exception:
                pass
        # swift_asr 识别进程
        r2 = _sp.run(["pgrep", "-f", "swift_asr"],
                     capture_output=True, text=True)
        for pid in r2.stdout.split():
            try:
                _sp.run(["kill", "-9", pid], timeout=5)
                cleaned_procs += 1
            except Exception:
                pass
        if cleaned_procs:
            print(f"[server] 启动清理: 清除 {cleaned_procs} 个残留子进程（sox/swift）", flush=True)
    except Exception as e:
        print(f"[server] 残留进程清理跳过: {e}", flush=True)

    # R1b: 本进程注册清理钩子（优雅退出时杀子进程）
    import atexit

    def _cleanup_children():
        try:
            import subprocess as _sp2
            _sp2.run(["pkill", "-9", "-f", "sox -q -d"], timeout=5)
            _sp2.run(["pkill", "-9", "-f", "swift_asr"], timeout=5)
        except Exception:
            pass
    atexit.register(_cleanup_children)

router = APIRouter()

STATIC_DIR = str(settings.STATIC_DIR)

RECORD_DIR = Path(MEETING_ROOT)
RECORD_DIR.mkdir(parents=True, exist_ok=True)


class StartBody(BaseModel):
    device: str = ""
    source: str = ""   # 可选：已有音频文件路径（走文件源，测试/转写用）
    asr_mode: str = "auto"   # auto=本地优先, local=Speech框架, cloud=百炼
    materials: list = []   # 可选：会议材料路径列表（会前准备，解析供纪要参考）


class StopBody(BaseModel):
    title: str = ""
    attendees: str = ""
    location: str = ""


class RegenBody(BaseModel):
    dir: str
    title: str = ""
    attendees: str = ""
    location: str = ""
    model: str = ""   # 空 = 默认模型
    materials: list = []   # 可选：会后补充材料路径
    template: str = "minutes"   # minutes=正式纪要 / summary=摘要 / bullets=要点 / analysis=深析


def _safe_task_dir(name: str) -> Path:
    """校验任务目录名，防路径穿越（单目录机制：YYYYMMDDNNN，兼容旧'日期+主题'与旧 tmp 数字目录）"""
    if not re.fullmatch(r"\d{11}", name):  # YYYYMMDDNNN（11 位）
        if not re.fullmatch(r"\d{10}", name):  # 旧 tmp 数字目录 YYYYMMDDNN
            if not re.fullmatch(r"\d{8}[^/\\:*?\"<>|]{1,30}", name):  # 旧归档 日期+主题
                raise HTTPException(400, "非法任务目录名")
    d = RECORD_DIR / name
    if not d.is_dir():
        raise HTTPException(404, f"任务不存在: {name}")
    return d


def _read_meta_title(d: Path) -> str:
    """读目录 metadata.json 的 topic/title 字段（历史列表显示用）。"""
    try:
        meta = json.load(open(d / "metadata.json", encoding="utf-8"))
        return meta.get("topic") or meta.get("title") or ""
    except Exception:
        return ""


def _list_tasks():
    """历史任务（dsh 单目录机制 2026-08-27）：MEETING_ROOT 下所有目录统一列出。
    成功标记 = 有 metadata.json（正常收口）；失败 = 有流水/录音但无 metadata（未收口/中断）。
    旧 tmp/ 目录（历史残留）也兼容列出。"""
    tasks = []
    for d in sorted(RECORD_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name == "tmp":
            continue
        files = [f.name for f in sorted(d.iterdir()) if f.is_file()]
        if not files:
            continue
        flow = d / "会议记录-流水.md"
        pcm = d / "会议录音.pcm"
        has_meta = (d / "metadata.json").exists()
        has_work = flow.exists() or pcm.exists()
        # 失败 = 有实质工作（流水/录音）但从未正常收口（无 metadata）
        is_failed = has_work and not has_meta
        n_sent = 0
        if flow.exists():
            for l in flow.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(r"^\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}", l.strip()):
                    n_sent += 1
        tasks.append({
            "dir": d.name,
            "title": _read_meta_title(d),
            "files": files,
            "failed": is_failed,
            "sentences": n_sent,
            "pcm_bytes": pcm.stat().st_size if pcm.exists() else 0,
            "modified": datetime.datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d %H:%M"),
        })
    return tasks


@router.get("/state")
async def state():
    rec = get_recorder()
    return rec.status()


@router.get("/sentences")
async def sentences():
    rec = get_recorder()
    return {"state": rec.state, "sentences": rec.sentence_list(),
            "summaries": list(rec.summaries),
            "partial": getattr(rec, "_partial_text", "")}


@router.get("/tasks")
async def tasks():
    return {"tasks": _list_tasks()}


@router.get("/devices")
async def devices():
    """列出 macOS 可用音频输入设备（SwitchAudioSource → CoreAudio 设备名）

    返回 [{"index": "<设备名>", "name": "<设备名>"}, ...]，前端下拉选择。
    注：采集器已从 ffmpeg avfoundation 迁移到 sox，设备改用 CoreAudio 设备名（不再是 avfoundation 索引 ":0"/":1"）。
    保留 index 字段仅为前端兼容，其值等于 name（设备名）。
    """
    try:
        r = subprocess.run(["SwitchAudioSource", "-a", "-t", "input"],
                           capture_output=True, text=True, timeout=10)
        names = [s.strip() for s in (r.stdout or "").splitlines() if s.strip()]
        # iPhone 优先（放会议中间拾音最准）
        names = sorted(names, key=lambda n: 0 if "iPhone" in n else 1)
        devs = [{"index": n, "name": n} for n in names]
        # 默认设备：当前系统默认输入（SwitchAudioSource -c）
        default = ""
        try:
            r2 = subprocess.run(["SwitchAudioSource", "-c", "-t", "input"],
                                capture_output=True, text=True, timeout=10)
            default = (r2.stdout or "").strip()
        except Exception:
            pass
        if not default and devs:
            default = devs[0]["name"]
        # 兜底：没有匹配到设备时
        if not devs:
            devs = [{"index": "MacBook Air麦克风", "name": "MacBook Air麦克风（默认）"},
                    {"index": "iPhone 麦克风", "name": "iPhone 麦克风（可选）"}]
            default = "MacBook Air麦克风"
        return {"devices": devs, "default": default}
    except Exception as e:
        return {"devices": [{"index": "MacBook Air麦克风", "name": "MacBook Air麦克风（默认）"},
                            {"index": "iPhone 麦克风", "name": "iPhone 麦克风（可选）"}],
                "default": "MacBook Air麦克风", "error": str(e)}


@router.post("/start")
async def start(body: StartBody):
    rec = get_recorder()
    ok, msg = rec.start(device=body.device or "", source=body.source or None,
                        asr_mode=body.asr_mode or "auto",
                        materials=body.materials or None)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "task_dir": msg, "state": rec.state,
            "asr_mode": rec.asr_mode}


class DeviceBody(BaseModel):
    device: str = ""


@router.post("/device")
async def switch_device(body: DeviceBody):
    """会议中手动切换录音设备（无感：仅重启 ffmpeg 采集源，转写不中断）。
    设备列表见 GET /api/devices（实时刷新）。"""
    rec = get_recorder()
    ok, msg = rec.switch_device(body.device)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "device": body.device, "msg": msg}


class MaterialsBody(BaseModel):
    paths: list = []


class InfoBody(BaseModel):
    title: str = ""
    attendees: str = ""
    location: str = ""
    host: str = ""
    secretary: str = ""


class ImportBody(BaseModel):
    text: str = ""        # 直接传转写稿文本
    file: str = ""        # 或传文件路径（.txt/.docx/.md，读取内容）
    extra: str = ""       # 补充信息（人工输入的纪要要求/自定义信息，LLM 理解后用于纪要）
    title: str = ""
    attendees: str = ""
    location: str = ""
    materials: list = []


@router.post("/import")
async def import_transcript(body: ImportBody):
    """导入外部转写稿（腾讯会议等）→ L2校对 → 实体核验 → 正式纪要 → 归档。"""
    rec = get_recorder()
    text = (body.text or "").strip()
    if not text and body.file:
        f = os.path.expanduser(body.file)
        if not os.path.exists(f):
            raise HTTPException(404, f"文件不存在: {body.file}")
        ext = os.path.splitext(f)[1].lower()
        if ext == ".docx":
            try:
                from docx import Document
                text = "\n".join(p.text for p in Document(f).paragraphs if p.text.strip())
            except Exception as e:
                raise HTTPException(400, f"docx 读取失败: {e}")
        else:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except Exception as e:
                raise HTTPException(400, f"文件读取失败: {e}")
    if not text.strip():
        raise HTTPException(400, "转写稿内容为空（text 或 file 至少提供一个）")
    # 同 /api/stop：导入走的是同一条"校对→核验→纪要→归档"阻塞链路，必须丢线程池
    ok, msg, files = await run_in_threadpool(
        lambda: rec.import_transcript(
            text, title=body.title, attendees=body.attendees,
            location=body.location, materials=body.materials or None,
            extra=body.extra))
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "task_dir": msg, "files": files, "last_error": rec.last_error}


@router.post("/recover")
async def recover_task(dir: str = ""):
    """R3: 中断任务一键恢复——把未收口任务（有流水/录音但无 metadata.json）走完整链路：
    恢复句子 → L2校对 → 实体核验 → 生成纪要 → 收尾 → 导出双库。
    不传 dir 时自动恢复所有可恢复任务（单目录机制 2026-08-27，兼容旧 tmp 路径）。"""
    rec = get_recorder()
    if rec.state != "idle":
        raise HTTPException(409, "当前有任务在运行，不能恢复")
    targets = []
    if dir:
        name = os.path.basename(dir)
        d = RECORD_DIR / name
        if not d.is_dir():
            d = RECORD_DIR / "tmp" / name  # 兼容旧 tmp 路径
        if not d.is_dir():
            raise HTTPException(404, f"没有该任务: {dir}")
        targets.append(d)
    else:
        for d in sorted(RECORD_DIR.iterdir(), reverse=True):
            if d.is_dir() and d.name != "tmp" and (d / "会议记录-流水.md").exists() \
                    and not (d / "metadata.json").exists():
                targets.append(d)
        # 兼容旧 tmp 残留
        tmp_root = RECORD_DIR / "tmp"
        if tmp_root.is_dir():
            for d in sorted(tmp_root.iterdir(), reverse=True):
                if d.is_dir() and (d / "会议记录-流水.md").exists():
                    targets.append(d)
    if not targets:
        return {"ok": True, "recovered": [], "msg": "没有可恢复的中断任务"}
    recovered = []
    for d in targets:
        try:
            flow_path = d / "会议记录-流水.md"
            lines = flow_path.read_text(encoding="utf-8").splitlines()
            sents = []
            for l in lines:
                m = re.match(r"^(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})\s+(.*)$", l.strip())
                if m:
                    sents.append({"ts": m.group(1), "rel_s": 0, "text": m.group(3), "speaker": ""})
            if not sents:
                continue
            rec.task_dir = str(d)
            rec.sentences = sents
            rec.files = {"transcript": str(flow_path)}
            rec._start_ts = 0
            rec._entity_verified = {}
            rec._entity_uncertain = []
            rec._clean_flow = ""
            # L2 校对
            try:
                from proofread import proofread_flow
                pr = proofread_flow(lines)
                if pr["clean_text"]:
                    rec._clean_flow = pr["clean_text"]
            except Exception:
                pass
            # 恢复流程同样会跑 LLM 纪要生成（分钟级），丢线程池避免占死事件循环
            minutes = await run_in_threadpool(
                lambda: rec._make_minutes(title="", attendees="", location=""))
            minutes_path = d / "会议纪要.md"
            minutes_path.write_text(minutes, encoding="utf-8")
            rec.files["minutes"] = str(minutes_path)
            archived, topic = finalize_archive(str(d), minutes)
            # 导出双库
            try:
                from export_minutes import export_all
                export_all(minutes, title="", task_dir=os.path.basename(archived))
            except Exception:
                pass
            # 2026-09-09 用户定稿：正式纪要生成后删录音（与 engine.stop 收口一致）
            try:
                for _name in ("会议录音.pcm", "会议录音.wav", "会议录音.mp3"):
                    _ap = os.path.join(archived, _name)
                    if os.path.exists(_ap):
                        os.remove(_ap)
            except Exception:
                pass
            recovered.append({"from": d.name, "to": os.path.basename(archived),
                              "sentences": len(sents), "minutes_chars": len(minutes)})
        except Exception as e:
            print(f"[server] 恢复任务 {d.name} 失败: {e}", flush=True)
    return {"ok": True, "recovered": recovered,
            "msg": f"已恢复 {len(recovered)} 个中断任务"}


@router.get("/broken_tasks")
async def broken_tasks():
    """列出未收口任务（有流水/录音但无 metadata.json，单目录机制 2026-08-27），
    供前端「出错任务」工具展示。每项含：目录名、句子数、录音大小、修改时间。"""
    out = []
    for d in sorted(RECORD_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name == "tmp":
            continue
        flow = d / "会议记录-流水.md"
        pcm = d / "会议录音.pcm"
        if not flow.exists() and not pcm.exists():
            continue
        if (d / "metadata.json").exists():
            continue  # 已正常收口，非 broken
        n_sent = 0
        if flow.exists():
            for l in flow.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.match(r"^\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}", l.strip()):
                    n_sent += 1
        out.append({
            "dir": d.name,
            "sentences": n_sent,
            "pcm_bytes": pcm.stat().st_size if pcm.exists() else 0,
            "modified": datetime.datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d %H:%M"),
            "has_flow": flow.exists(),
        })
    return {"ok": True, "tasks": out}


class ContinueBody(BaseModel):
    dir: str
    device: str = ""
    asr_mode: str = "auto"


@router.post("/continue")
async def continue_task(body: ContinueBody):
    """断点续传：复用 tmp 下出错任务目录继续录音（追加 pcm/流水，不丢已转写内容）。"""
    rec = get_recorder()
    if rec.state != "idle":
        raise HTTPException(409, f"当前状态 {rec.state}，不能续传（先结束当前任务）")
    # 校验目录（单目录机制：MEETING_ROOT 下；兼容旧 tmp 路径）
    cd = os.path.join(RECORD_DIR, os.path.basename(body.dir))
    if not os.path.isdir(cd):
        cd = os.path.join(RECORD_DIR, "tmp", os.path.basename(body.dir))
    if not os.path.isdir(cd):
        raise HTTPException(404, f"没有该任务: {body.dir}")
    ok, msg = rec.start(device=body.device or "", asr_mode=body.asr_mode or "auto",
                        continue_dir=cd)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "task_dir": msg, "state": rec.state,
            "resumed_sentences": len(rec.sentences)}


class DeleteBody(BaseModel):
    dir: str


@router.post("/delete")
async def delete_task(body: DeleteBody):
    """F1: 删除历史会议（meeting/ 下归档目录或 tmp 残留）。
    同时删除 WIKI meetings/ 和 Obsidian Inbox 的同步副本。"""
    dname = os.path.basename(body.dir)
    deleted = []
    # 1. meeting/ 主目录（归档或 tmp）
    for root in (RECORD_DIR, RECORD_DIR / "tmp"):
        d = root / dname
        if d.is_dir():
            import shutil
            shutil.rmtree(d)
            deleted.append(str(d))
    # 2. 知识库 meetings 同步副本（日期前缀匹配）；未配置知识库目录时跳过
    wiki_dir = settings.wiki_meetings_dir()
    if wiki_dir and wiki_dir.is_dir():
        for f in wiki_dir.iterdir():
            # 匹配 "YYYY-MM-DD-<主题>-会议纪要.md" 且主题含目录名（去日期部分）
            if f.is_file() and dname[8:] and dname[8:] in f.stem:
                f.unlink(missing_ok=True)
                deleted.append(str(f))
    # 3. Obsidian 收件箱同步副本
    ob_dir = settings.obsidian_inbox()
    if ob_dir and ob_dir.is_dir():
        for f in ob_dir.iterdir():
            if f.is_file() and dname[8:] and dname[8:] in f.stem:
                f.unlink(missing_ok=True)
                deleted.append(str(f))
    if not deleted:
        raise HTTPException(404, f"未找到任务目录: {dname}")
    return {"ok": True, "deleted": deleted}


@router.post("/info")
async def set_info(body: InfoBody):
    """任意时刻（录音前/中/后）填写/更新会议信息（主题/参会/地点）。
    可选操作，不阻塞主流程；结束时自动采用最新值。"""
    rec = get_recorder()
    ok, info = rec.set_info(title=body.title, attendees=body.attendees,
                            location=body.location, host=body.host,
                            secretary=body.secretary)
    return {"ok": ok, "info": info}


@router.get("/info")
async def get_info():
    """读取当前会议信息（含录音中已填写的）"""
    rec = get_recorder()
    return {"ok": True, "info": rec.get_info()}


@router.post("/materials")
async def add_materials(body: MaterialsBody):
    """任意时刻（录音前/中/后）提交会议材料。旁路操作：后台解析，不阻塞/不影响主流程。
    提交后立即可继续会议记录；解析完成供纪要生成参考。"""
    rec = get_recorder()
    # 路径由用户手填，统一在这里展开 `~`（别在页面里硬编码 home）
    paths = [os.path.expanduser(p) for p in (body.paths or [])]
    n, msg = rec.add_materials(paths)
    if n == 0:
        return {"ok": True, "added": 0, "msg": msg}
    return {"ok": True, "added": n, "msg": msg}


class NoteBody(BaseModel):
    text: str = ""


class NoteDeleteBody(BaseModel):
    index: int = -1


class NoteUpdateBody(BaseModel):
    index: int = -1
    text: str = ""


@router.post("/notes")
async def add_note(body: NoteBody):
    """对话式补充信息（旁白/叮嘱）：随时追加一条，落盘任务目录 补充信息.md，
    纪要生成时注入。提交一句保存一句。"""
    rec = get_recorder()
    ok, notes = rec.add_note(body.text)
    if not ok:
        raise HTTPException(400, "补充信息不能为空")
    return {"ok": True, "notes": notes, "count": len(notes)}


@router.get("/notes")
async def get_notes():
    """读取当前会议已提交的补充信息列表（含录音中已提交的）"""
    rec = get_recorder()
    return {"ok": True, "notes": rec.get_notes()}


@router.post("/notes/delete")
async def delete_note(body: NoteDeleteBody):
    """删除一条补充信息（按序号）"""
    rec = get_recorder()
    ok, notes = rec.delete_note(body.index)
    if not ok:
        raise HTTPException(404, "补充信息序号无效")
    return {"ok": True, "notes": notes}


@router.post("/notes/update")
async def update_note(body: NoteUpdateBody):
    """修改一条补充信息（用户补充可能有误，可修正）"""
    rec = get_recorder()
    ok, notes = rec.update_note(body.index, body.text)
    if not ok:
        raise HTTPException(404, "补充信息序号无效或内容为空")
    return {"ok": True, "notes": notes}


@router.post("/pause")
async def pause():
    rec = get_recorder()
    ok, msg = rec.pause()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "state": rec.state}


@router.post("/resume")
async def resume():
    rec = get_recorder()
    ok, msg = rec.resume()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "state": rec.state}


@router.post("/stop")
async def stop(body: StopBody, wait: int = 0):
    """结束会议。

    默认**异步**（2026-09-13 起）：立即返回 `state=processing`，把
    "停录→L2校对→实体核验→纪要生成→归档导出"整套流程丢到后台线程跑。
    进度与结果通过 `GET /api/state` 的 `job` 字段读取（前端本来就每 2 秒轮询）。
    想等结果再返回（脚本/调试）加 `?wait=1`。
    """
    rec = get_recorder()
    if wait:
        ok, msg, files = await run_in_threadpool(
            lambda: rec.stop(title=body.title, attendees=body.attendees,
                             location=body.location))
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True, "async": False, "task_dir": msg, "files": files,
                "last_error": rec.last_error}
    if not rec.mark_processing():
        j = rec.job_info() or {}
        extra = f"（后台正在跑 {j.get('kind')}）" if j.get("running") else ""
        raise HTTPException(409, f"当前状态 {rec.state}，没有进行中的录音{extra}")
    started, info = rec.start_job(
        "stop", lambda: rec.stop(title=body.title, attendees=body.attendees,
                                 location=body.location))
    if not started:
        raise HTTPException(409, str(info))
    return {"ok": True, "async": True, "state": "processing",
            "task_dir": rec.task_dir, "files": {}, "last_error": "",
            "hint": "后台整理中；进度看 GET /api/state 的 job 字段，完成后 state 回到 idle"}


def _regen_minutes_impl(body: RegenBody):
    """补生成/重生成纪要的**同步实现**。

    流程：读流水 → 重建句子 → 读补充信息 → L2 校对 → 材料解析 → LLM 生成 → 覆盖写回 → 双库导出。
    由 `/api/regen_minutes` 调用：默认丢后台线程跑（立即返回），`?wait=1` 则同步等结果。
    """
    d = _safe_task_dir(body.dir)
    transcript = d / "会议记录-流水.md"
    if not transcript.exists():
        raise HTTPException(404, "该任务没有流水文件")
    sents = []
    for l in transcript.read_text(encoding="utf-8").splitlines():
        # 兼容：旧格式 "[HH:MM:SS] 文字" / 新格式 "HH:MM:SS-HH:MM:SS  文字" / 标记行跳过
        if l.startswith("#") or l.startswith("---") or l.strip().startswith("##"):
            continue
        m = re.match(r"^\[?(\d{2}:\d{2}:\d{2})\]?(?:-\d{2}:\d{2}:\d{2})?\s+(?:\[说话人\])?\s*(.*)$", l)
        if m:
            sents.append({"ts": m.group(1), "text": m.group(2).strip()})
    if not sents:
        raise HTTPException(400, "流水文件没有可识别的句子")
    rec = MeetingRecorder()   # 独立实例，不影响当前录音状态
    rec.sentences = sents
    # 会后重写：读任务目录 补充信息.md（会中旁白/叮嘱）注入纪要生成
    try:
        notes_f = d / "补充信息.md"
        if notes_f.exists():
            rec.notes = []
            for l in notes_f.read_text(encoding="utf-8").splitlines():
                l = l.strip()
                if l.startswith("[") and "]" in l:
                    ts, _, text = l[1:].partition("]")
                    text = text.strip()
                    if text:
                        rec.notes.append({"ts": ts.strip(), "text": text})
    except Exception:
        pass
    # L2 校对：regen 也走清洗稿（读流水原文件行，保证纠错一致）
    try:
        from proofread import proofread_flow
        flow_lines = transcript.read_text(encoding="utf-8").splitlines()
        pr = proofread_flow(flow_lines)
        if pr["clean_text"]:
            rec._clean_flow = pr["clean_text"]
    except Exception:
        pass
    # 会后补充材料：请求传入 + 归档目录 materials/ 子目录（如有）
    mat_paths = list(body.materials or [])
    mat_dir = d / "materials"
    if mat_dir.is_dir():
        mat_paths.extend(str(p) for p in sorted(mat_dir.iterdir())
                         if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".txt", ".md", ".pptx"))
    if mat_paths:
        try:
            rec.add_materials(mat_paths)
            import time as _t
            _t.sleep(1.5)   # 等后台解析（regen 场景可容忍短暂等待）
        except Exception:
            pass
    model = body.model or LLM_MODEL
    try:
        if body.template == "weekly":
            # 周际待办联动：找上一期纪要（同一根目录下日期较早的最近一期）
            prev_path = None
            try:
                candidates = []
                for dd in sorted(RECORD_DIR.iterdir(), reverse=True):
                    if not dd.is_dir() or dd == d or dd.name == "tmp":
                        continue
                    f = dd / "会议纪要.md"
                    if f.exists():
                        candidates.append(f)
                    if len(candidates) >= 3:
                        break
                if candidates:
                    prev_path = str(candidates[0])
            except Exception:
                pass
            minutes = rec._make_weekly(title=body.title, attendees=body.attendees,
                                       location=body.location,
                                       prev_minutes_path=prev_path, model=model)
        elif body.template and body.template != "minutes":
            minutes = rec._make_by_template(body.template, title=body.title,
                                            attendees=body.attendees,
                                            location=body.location, model=model)
        else:
            minutes = rec._make_minutes(title=body.title, attendees=body.attendees,
                                        location=body.location, model=model)
    except Exception as e:
        raise HTTPException(500, f"纪要生成失败({model}): {e}")
    out = d / "会议纪要.md"
    out.write_text(minutes, encoding="utf-8")
    extra = {}
    try:   # 重写后同步覆盖 Obsidian + WIKI
        from export_minutes import export_all
        ex = export_all(minutes, body.title, task_dir=os.path.basename(str(d)))
        extra = {"obsidian": ex["obsidian"], "wiki": ex["wiki"]}
    except Exception as e:
        extra = {"export_error": str(e)}
    return {"ok": True, "dir": body.dir, "model": model, "minutes": minutes, **extra}


@router.post("/regen_minutes")
async def regen_minutes(body: RegenBody, wait: int = 0):
    """对已完成任务补生成/重生成会议纪要。

    默认**异步**（2026-09-13 起）：立即返回，后台线程跑"读流水→校对→LLM→写回→导出"；
    进度与结果看 `GET /api/state` 的 `job` 字段。
    想等结果再返回加 `?wait=1`（返回体与旧版一致）。
    """
    if wait:
        try:
            return await run_in_threadpool(lambda: _regen_minutes_impl(body))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"纪要生成失败({body.model or LLM_MODEL}): {e}")
    rec = get_recorder()
    started, info = rec.start_job("regen_minutes",
                                  lambda: _regen_minutes_impl(body))
    if not started:
        raise HTTPException(409, str(info))
    return {"ok": True, "async": True, "state": "processing", "dir": body.dir,
            "hint": "后台重生成中；进度与结果看 GET /api/state 的 job 字段"}


class SpeakersBody(BaseModel):
    dir: str = ""
    model: str = ""


class SpeakersApplyBody(BaseModel):
    dir: str
    mapping: dict = {}   # {"说话人0": "张三", ...}


@router.post("/speakers")
async def suggest_speakers(body: SpeakersBody):
    """F：说话人映射建议（MeetMemo 设计吸收）——LLM 结合人名职责索引，
    为流水中的'说话人N'建议真实人名。返回映射供用户确认。"""
    rec = get_recorder()
    # 取当前录音的句子，或归档目录流水
    sents = rec.sentences if rec.sentences else []
    flow_text = ""
    if body.dir:
        d = _safe_task_dir(body.dir)
        f = d / "会议记录-清洗稿.md"
        if not f.exists():
            f = d / "会议记录-流水.md"
        if f.exists():
            flow_text = f.read_text(encoding="utf-8")
    if not flow_text and sents:
        flow_text = "\n".join(f"[{s.get('ts')}] {s.get('text')}" for s in sents)
    if not flow_text or "说话人" not in flow_text:
        raise HTTPException(400, "流水中没有说话人标签（可能未开启说话人分离）")
    idx_path = settings.speaker_index()
    idx_text = ""
    if idx_path and os.path.exists(idx_path):
        try:
            idx_text = open(idx_path, encoding="utf-8").read()
        except Exception:
            pass
    prompt = (
        "你是会议说话人识别助手。根据发言内容和【人名职责索引】判断每个'说话人N'可能是谁。"
        "只输出 JSON 对象映射：{\"说话人0\": \"姓名\", ...}。"
        "无法确定的保留原标签；姓名用全名。"
    )
    if idx_text:
        prompt += f"\n\n【人名职责索引】\n{idx_text[:4000]}"
    try:
        out = rec._chat(prompt, flow_text[:18000], max_tokens=500,
                        model=body.model or LLM_MODEL)
        import re as _re
        m = _re.search(r"\{.*\}", out, _re.S)
        mapping = json.loads(m.group(0)) if m else {}
        return {"ok": True, "suggestions": mapping}
    except Exception as e:
        raise HTTPException(500, f"说话人建议失败: {e}")


@router.post("/speakers/apply")
async def apply_speakers(body: SpeakersApplyBody):
    """应用说话人映射：替换流水/清洗稿中的'说话人N'为真实人名，重写文件。"""
    d = _safe_task_dir(body.dir)
    mapping = body.mapping or {}
    if not mapping:
        raise HTTPException(400, "映射为空")
    changed = 0
    for fname in ("会议记录-清洗稿.md", "会议记录-流水.md"):
        f = d / fname
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        new_text = text
        for tag, name in mapping.items():
            if tag and name:
                new_text = new_text.replace(tag, name)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
            changed += 1
    return {"ok": True, "changed_files": changed}


# 纪要预览的文档渲染样式（浅色主题，参照 doc-preview）
PREVIEW_CSS = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
     line-height:1.7;color:#24292f;background:#fff;padding:28px 36px;
     max-width:900px;margin:0 auto}
h1,h2,h3{border-bottom:1px solid #eaeef2;padding-bottom:6px;margin-top:1.4em}
h1{font-size:1.6em}h2{font-size:1.3em}h3{font-size:1.1em}
table{border-collapse:collapse;margin:12px 0;width:100%}
th,td{border:1px solid #d0d7de;padding:6px 10px;font-size:13px;text-align:left}
th{background:#f6f8fa}
code{background:#f6f8fa;padding:2px 5px;border-radius:4px;font-size:12.5px}
pre{background:#f6f8fa;padding:12px;border-radius:6px;overflow:auto}
blockquote{border-left:4px solid #d0d7de;margin:0;padding:0 12px;color:#57606a}
ul,ol{padding-left:24px}
"""


def _render_md_html(md_file: Path, fallback: str = "") -> str:
    """md 文件 → 完整 HTML 文档（浅色主题，供前端 iframe srcdoc 渲染）"""
    try:
        import markdown as md_lib
        text = md_file.read_text(encoding="utf-8", errors="replace")
        body_html = md_lib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception as e:
        body_html = f"<p>渲染失败：{html_lib.escape(str(e))}</p>"
    return (f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            f"<title>{html_lib.escape(md_file.name)}</title>"
            f"<style>{PREVIEW_CSS}</style></head><body>{body_html}"
            f"<p style='color:#999;font-size:12px'>{html_lib.escape(fallback)}</p></body></html>")


# 纪要预览白名单文件
PREVIEW_FILES = ("会议纪要.md", "会议记录-流水.md", "会议记录-清洗稿.md")


@router.get("/preview")
async def preview(dir: str = Query(...), file: str = Query("会议纪要.md")):
    """通用 md 渲染预览（纪要或流水），file 白名单防路径穿越"""
    d = _safe_task_dir(dir)
    if file not in PREVIEW_FILES:
        raise HTTPException(400, "不允许预览该文件")
    p = d / file
    if not p.exists():
        raise HTTPException(404, f"文件不存在: {file}")
    return {"kind": "html", "title": file, "html": _render_md_html(p)}


@router.get("/flow")
async def get_flow(dir: str = Query(...)):
    """结构化流水：解析流水 md → [{ts_start, speaker, text}]（前端渲染 + 音频同步用）。
    优先读清洗稿（L2 校对后版本），无清洗稿才读原始流水。"""
    d = _safe_task_dir(dir)
    f = d / "会议记录-清洗稿.md"
    if not f.exists():
        f = d / "会议记录-流水.md"
    if not f.exists():
        return {"ok": True, "sentences": []}
    sents = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        m = re.match(r"^(\d{1,2}:\d{2}:\d{2})-(\d{1,2}:\d{2}:\d{2})\s+(.*)$", line)
        if m:
            body = m.group(3).strip()
            spk = ""
            m2 = re.match(r"^\[([^\]]+)\]\s*(.*)$", body)
            if m2:
                cand = m2.group(1)
                # 过滤原始时间戳 [MM:SS-MM:SS]（腾讯会议转写带进），不是说话人
                if not re.fullmatch(r"\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?", cand):
                    spk = cand
                    body = m2.group(2)
                else:
                    body = m2.group(2)
            # 解析起始秒（同步高亮用）
            parts = m.group(1).split(":")
            ts_start = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            sents.append({"ts": m.group(1), "start": ts_start, "speaker": spk, "text": body})
    return {"ok": True, "sentences": sents}


@router.get("/preview_minutes")
async def preview_minutes(dir: str = Query(...)):
    """兼容别名：预览纪要（无纪要时回退流水）"""
    d = _safe_task_dir(dir)
    md_file = d / "会议纪要.md"
    fallback = ""
    if not md_file.exists():
        md_file = d / "会议记录-流水.md"
        fallback = "（尚未生成纪要，预览流水）"
    if not md_file.exists():
        raise HTTPException(404, "该任务没有可预览的文档")
    return {"kind": "html", "title": md_file.name,
            "html": _render_md_html(md_file, fallback)}


# 可用对话模型（重写纪要时选择）。优先排序 + 自动过滤非对话模型
MODEL_PREFERENCE = [
    "deepseek-flash",              # 本项目默认纪要模型（用户指定，2026-09-13）
    "qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "glm-5.2", "ZHIPU/GLM-5.2", "kimi-k2.7",
    "MiniMax/MiniMax-M3", "kimi/kimi-k2.7",
]
MODEL_EXCLUDE = ("image", "audio", "asr", "ocr", "embedding", "realtime",
                 "translate", "whisper", "tts", "speech", "video",
                 "livetranslate", "flash-20", "test-sre", "gpu-auto")


@router.get("/models")
async def models():
    """返回可用的对话模型列表（重写纪要的模型切换）"""
    base = settings.dashscope_compatible_url()
    ids = []
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            base + "/models",
            headers={"Authorization": f"Bearer {load_api_key()}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode("utf-8"))
        ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except Exception:
        pass
    out = [m for m in MODEL_PREFERENCE if m in ids]
    for m in sorted(ids):
        if m in out or any(x in m for x in MODEL_EXCLUDE):
            continue
        out.append(m)
        if len(out) >= 15:
            break
    # 合并本地 Agent 配置里已声明的模型（可选；$MEETING_AGENT_CONFIG 指定，默认探测常见位置）
    try:
        import yaml
        cfg_path = os.environ.get("MEETING_AGENT_CONFIG")
        cfg = None
        for _p in ([cfg_path] if cfg_path else []) + [
                os.path.expanduser("~/.codex/config.toml")]:
            if not _p or not os.path.exists(_p):
                continue
            if _p.endswith(".toml"):
                try:
                    import tomllib
                    cfg = tomllib.load(open(_p, "rb"))
                except Exception:
                    cfg = None
            else:
                cfg = yaml.safe_load(open(_p, encoding="utf-8"))
            if cfg:
                break
        extra = []
        mm = (cfg or {}).get("model", {}) or {}
        if isinstance(mm, str):
            mm = {"default": mm}
        if mm.get("default"):
            extra.append(mm["default"])
        for fp in (cfg or {}).get("fallback_providers", []) or []:
            if isinstance(fp, dict) and fp.get("model"):
                extra.append(fp["model"])
        for x in extra:
            if x and x not in out:
                out.append(x)
    except Exception:
        pass
    # 下拉框默认值跟随实际生效的纪要模型（LLM_MODEL），避免"选着 A 实际用 B"
    if LLM_MODEL in out:            # 并把默认模型排到首位，UI 一眼可见
        out.remove(LLM_MODEL)
        out.insert(0, LLM_MODEL)
    default = LLM_MODEL if LLM_MODEL in out else (out[0] if out else "")
    return {"models": out, "default": default}


@router.get("/audio")
async def get_audio(dir: str = Query(...)):
    """返回会议录音 mp3 文件（供前端播放器用）。wav 兜底（历史任务可能只有 wav）。"""
    d = _safe_task_dir(dir)
    for name in ("会议录音.mp3", "会议录音.wav"):
        p = d / name
        if p.exists():
            media = "audio/mpeg" if name.endswith(".mp3") else "audio/wav"
            return FileResponse(p, media_type=media)
    raise HTTPException(404, "该任务没有录音文件")


@router.get("/open")
async def open_file(dir: str = Query(...), file: str = Query(...)):
    """用 macOS 默认应用打开任务产物（白名单防路径穿越）"""
    d = _safe_task_dir(dir)
    if file not in ("会议纪要.md", "会议记录-流水.md", "会议录音.wav", "会议录音.pcm"):
        raise HTTPException(400, "不允许打开该文件")
    p = d / file
    if not p.exists():
        raise HTTPException(404, f"文件不存在: {file}")
    try:
        subprocess.Popen(["open", str(p)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")
    return {"ok": True}


@router.get("/task")
async def task(dir: str = Query(..., description="任务目录名，如 2026081301")):
    d = _safe_task_dir(dir)
    out = {"dir": d.name, "files": []}
    for f in sorted(d.iterdir()):
        if f.is_file():
            out["files"].append(f.name)
    transcript = d / "会议记录-流水.md"
    if transcript.exists():
        out["transcript"] = transcript.read_text(encoding="utf-8")[:50000]
    minutes = d / "会议纪要.md"
    if minutes.exists():
        out["minutes"] = minutes.read_text(encoding="utf-8")[:30000]
        out["minutes_html"] = _render_md_html(minutes)
    wav = d / "会议录音.wav"
    if wav.exists():
        out["wav_bytes"] = wav.stat().st_size
    mp3 = d / "会议录音.mp3"
    if mp3.exists():
        out["mp3_bytes"] = mp3.stat().st_size
    return out


# ── 纪要导出（MeetMemo 设计吸收：PDF）────────────────
def _content_disposition(filename: str) -> str:
    """构造 ASCII 安全的 Content-Disposition。

    HTTP 头只能装 latin-1，中文文件名（如"20260828001-纪要.pdf"）直接放进 filename=
    会在 starlette 编码响应头时抛 UnicodeEncodeError → 导出整条链路 500。
    这里给 ASCII 兜底名 + RFC 5987 的 filename*（浏览器优先用它，中文名照样正确）。
    """
    from urllib.parse import quote
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
    return (f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(filename)}")


@router.get("/export")
async def export_meeting(dir: str = "", fmt: str = "pdf"):
    """导出会议纪要：fmt=pdf（pandoc 渲染，中文需系统中文字体）或 md（原样返回）。
    返回文件下载。"""
    d = _safe_task_dir(dir)
    src = d / "会议纪要.md"
    if not src.exists():
        src = d / "会议记录-清洗稿.md"
    if not src.exists():
        raise HTTPException(404, "该任务没有纪要/清洗稿可导出")
    text = src.read_text(encoding="utf-8")
    if fmt == "md":
        return Response(content=text, media_type="text/markdown",
                        headers={"Content-Disposition": _content_disposition(f"{d.name}-纪要.md")})
    # PDF：优先 pandoc+xelatex，失败回退 weasyprint
    pdf_bytes = _md_to_pdf_pandoc(src) or _md_to_pdf_weasy(text)
    if not pdf_bytes:
        raise HTTPException(500, "PDF 生成失败（pandoc/xelatex 与 weasyprint 均不可用）")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": _content_disposition(f"{d.name}-纪要.pdf")})


def _md_to_pdf_pandoc(src: Path) -> bytes:
    """md → PDF（pandoc + xelatex）。

    ⚠️ 2026-09-13 修正的两个坑：
    ① 必须输出到**临时 .pdf 文件**而不是 `-o -`：pandoc 按输出文件扩展名判断目标格式，
       写到 stdout 时没有扩展名 → 它**默认产出 HTML**，而且**退出码仍是 0**，
       于是"非空即当 PDF"的写法会把一段 HTML 当成 PDF 发出去（静默坏掉）。
    ② 因此返回前**校验 %PDF- 魔数**，不是真 PDF 一律返回 b"" 交给上层回退。
    """
    import os as _os
    import subprocess as sp
    import tempfile
    if not _os.path.exists("/opt/homebrew/bin/pandoc"):
        return b""
    try:
        with tempfile.TemporaryDirectory() as td:
            out = _os.path.join(td, "out.pdf")
            sp.run(
                ["/opt/homebrew/bin/pandoc", str(src), "-o", out,
                 "--pdf-engine=xelatex",
                 "-V", "CJKmainfont=PingFang SC",
                 "-V", "geometry:margin=2.5cm",
                 "-V", "mainfont=PingFang SC"],
                capture_output=True, timeout=90)
            if not _os.path.exists(out):
                return b""
            data = Path(out).read_bytes()
            return data if data[:5] == b"%PDF-" else b""
    except Exception:
        return b""


def _md_to_pdf_weasy(text: str) -> bytes:
    """md → HTML → PDF（weasyprint，无需 latex）"""
    try:
        import markdown as md_lib
        import weasyprint
    except Exception:
        # 2026-09-19 放宽异常类型：裸解释器下 `import weasyprint` 会抛 OSError
        # （dlopen 找不到 libgobject-2.0-0）。正常启动由 runtime.ensure_native_libs()
        # 带 DYLD_FALLBACK_LIBRARY_PATH 重入后再 import，故能正常工作；但本函数若被
        # 单独调用（未经该引导），OSError 会绕过原来的 `except ImportError` 冒泡成 500。
        # 捕获所有异常 → 按设计返回 b""，由调用方抛「两种引擎均不可用」的友好 500。
        return b""
    html_body = md_lib.markdown(text, extensions=["tables", "fenced_code"])
    html = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>body{{font-family:'PingFang SC','Heiti SC',sans-serif;"
            f"font-size:13px;line-height:1.7;padding:24px;}}"
            f"table{{border-collapse:collapse;width:100%}}"
            f"th,td{{border:1px solid #999;padding:4px 8px;font-size:12px}}</style>"
            f"</head><body>{html_body}</body></html>")
    try:
        from io import BytesIO
        buf = BytesIO()
        weasyprint.HTML(string=html).write_pdf(buf)
        return buf.getvalue()
    except Exception:
        return b""


# ── 历史会议检索（minutes-search 设计吸收）────────────
@router.get("/search")
async def search_meetings(q: str = ""):
    """跨会议全文检索：在 meeting/ 下所有纪要/流水/清洗稿中搜关键词。
    返回匹配的会议 + 命中片段（纪要优先）。"""
    q = (q or "").strip()
    if not q:
        return {"ok": True, "results": []}
    results = []
    try:
        for d in sorted(RECORD_DIR.iterdir(), reverse=True):
            if not d.is_dir() or d.name == "tmp":
                continue
            # 纪要 > 清洗稿 > 流水（优先级）
            files = [d / "会议纪要.md", d / "会议记录-清洗稿.md", d / "会议记录-流水.md"]
            hit = None
            for f in files:
                if f.exists():
                    try:
                        text = f.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if q.lower() in text.lower():
                        # 提取命中片段（上下文 ±60 字符）
                        idx = text.lower().find(q.lower())
                        snippet = text[max(0, idx - 60):idx + len(q) + 60].replace("\n", " ")
                        hit = {"file": f.name, "snippet": snippet}
                        break
            if hit:
                results.append({"dir": d.name, **hit})
            if len(results) >= 20:
                break
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "results": results}


# ── 静态前端 ────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    """会议记录前端页面（仿豆包）"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.include_router(router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    # 端口：命令行参数 > $MEETING_PORT > 8789
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
        else int(os.environ.get("MEETING_PORT") or 8789)
    uvicorn.run(app, host="127.0.0.1", port=port)
