#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runtime.py — 解释器探测（纯标准库，Python 3.9+ 可跑）

本项目的服务与脚本统一通过这里决定「用哪个 Python 跑」，不再写死任何 AI Agent
平台的私有路径。历史上这里写死过某平台的 venv，平台一变服务就整片起不来。

解析优先级（先到先得，逐个实测依赖是否齐全）：
  1. $MEETING_SERVER_PYTHON      显式指定，永远最高优先级
  2. <项目目录>/.venv/bin/python   项目本地虚拟环境
  3. $AI_AGENT_PYTHON            跨平台通用约定变量
  4. $VIRTUAL_ENV
  5. 已知位置探测（Codex / TRAE / Hermes 遗留 / uv / 用户级 venv）
  6. PATH 上的 python3

判据不是「路径存在」，而是「必需模块能不能 import 到」——所以即使换机器、
换 Agent 平台，只要环境里装齐了依赖就能自动命中。

CLI:
  python3 runtime.py            打印解析到的解释器路径
  python3 runtime.py --check    解析 + 校验依赖，缺失时退出码 1
  python3 runtime.py --list     列出全部候选，以及各自缺哪些模块
  python3 runtime.py --doctor   完整诊断（解释器 / 依赖 / 可选数据源）
"""
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = "meeting-server"
ENV_PREFIX = "MEETING_SERVER"          # 对应 $MEETING_SERVER_PYTHON
MIN_VERSION = (3, 9)
PROJECT_DIR = Path(__file__).resolve().parent

# 必需模块用「import 名」而非「包名」：websocket-client→websocket、PyMuPDF→fitz…
REQUIRED_MODULES = (
    "fastapi", "uvicorn", "pydantic", "starlette",
    "dashscope", "websocket", "pypinyin",
    "docx", "pptx", "openpyxl", "fitz",
    "markdown", "weasyprint", "yaml",
)

_PROBE_SRC = (
    "import importlib.util, json, sys\n"
    "missing = []\n"
    "for m in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        if importlib.util.find_spec(m) is None:\n"
    "            missing.append(m)\n"
    "    except Exception:\n"
    "        missing.append(m)\n"
    "print(json.dumps(missing))\n"
)

# 已知候选位置；{home} 展开为用户主目录，{project} 为本项目名
CANDIDATE_PATTERNS = (
    ("Codex 共享环境", "{home}/.codex/venv/bin/python"),
    ("Codex 用途环境", "{home}/.codex/venvs/{project}/bin/python"),
    ("Codex 其他环境", "{home}/.codex/venvs/*/bin/python"),
    ("Hermes 共享环境", "{home}/.hermes/venv/bin/python3"),
    ("DSH 共享环境", "{home}/.dsh/venv/bin/python3"),
    ("TRAE 环境", "{home}/.trae-cn/**/venv/bin/python"),
    ("用户 ~/n", "{home}/n/bin/python3"),
    ("用户级 venv", "{home}/.venv/bin/python"),
    ("uv 托管 Python", "{home}/.local/share/uv/python/*/bin/python3"),
)


def _run(argv, timeout=20):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def version_of(python):
    """返回 (major, minor)；不可用时返回 None。"""
    try:
        r = _run([str(python), "-c",
                  "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=15)
        if r.returncode != 0:
            return None
        major, minor = r.stdout.strip().split(".")
        return int(major), int(minor)
    except Exception:
        return None


def missing_modules(python, modules=REQUIRED_MODULES):
    """列出该解释器里 import 不到的模块（只查 spec，不执行模块，快且无副作用）。"""
    try:
        r = _run([str(python), "-c", _PROBE_SRC, json.dumps(list(modules))], timeout=30)
        if r.returncode != 0:
            return list(modules)
        return json.loads(r.stdout.strip() or "[]")
    except Exception:
        return list(modules)


def satisfied(python, modules=REQUIRED_MODULES):
    v = version_of(python)
    if v is None or v < MIN_VERSION:
        return False
    return not missing_modules(python, modules)


def candidates():
    """按优先级返回候选解释器 [(说明, 路径)]，去重且只保留可执行文件。"""
    seen, out = set(), []

    def add(path, label):
        if not path:
            return
        p = Path(os.path.expanduser(str(path)))
        if p.is_dir():                      # 允许直接给 venv 目录
            p = p / "bin" / "python"
        key = str(p)
        if key in seen or not p.is_file() or not os.access(str(p), os.X_OK):
            return
        seen.add(key)
        out.append((label, p))

    add(os.environ.get(f"{ENV_PREFIX}_PYTHON"), f"${ENV_PREFIX}_PYTHON")
    add(PROJECT_DIR / ".venv" / "bin" / "python", "项目本地 .venv")
    add(os.environ.get("AI_AGENT_PYTHON"), "$AI_AGENT_PYTHON")
    add(os.environ.get("VIRTUAL_ENV"), "$VIRTUAL_ENV")

    home = str(Path.home())
    for label, pattern in CANDIDATE_PATTERNS:
        pat = pattern.format(home=home, project=PROJECT)
        for hit in sorted(glob.glob(pat, recursive=True)):
            add(hit, label)

    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            p = Path(d) / name
            if p.is_file():
                add(p, f"PATH 上的 {name}")
                break
    return out


def resolve(modules=REQUIRED_MODULES, strict=False):
    """返回应当使用的解释器路径。

    strict=False：没有任何候选满足依赖时退回优先级最高的候选（不抛错），
                  调用方可据此打印修复指引。
    strict=True ：找不到满足依赖的解释器就抛 RuntimeError。
    """
    cands = candidates()
    if not cands:
        raise RuntimeError("找不到任何可用的 Python 解释器")
    for _label, path in cands:
        if satisfied(path, modules):
            return path
    if strict:
        raise RuntimeError(
            "没有找到依赖齐全的 Python。已探测：\n  "
            + "\n  ".join(f"{lbl}: {p}" for lbl, p in cands)
            + "\n修复：安装依赖后重试——\n  python3 -m pip install -r "
            + str(PROJECT_DIR / "requirements.txt")
        )
    return cands[0][1]


def ensure_runtime(modules=REQUIRED_MODULES, argv=None):
    """当前解释器缺依赖时，用解析到的解释器重新执行本进程（只重入一次，防死循环）。"""
    miss = missing_modules(sys.executable, modules)
    if not miss:
        return sys.executable
    guard = f"{ENV_PREFIX}_REEXEC"
    if os.environ.get(guard) == "1":
        sys.stderr.write(
            f"[runtime] 警告：当前解释器 {sys.executable} 缺少 {miss}，"
            f"且已重入过一次，继续用当前环境启动。\n"
            f"[runtime] 修复：python3 -m pip install -r "
            f"{PROJECT_DIR / 'requirements.txt'}\n")
        return sys.executable
    target = resolve(modules)
    if Path(target).resolve() == Path(sys.executable).resolve():
        return sys.executable
    rest = _reexec_argv(argv)
    if rest is None:      # 无法安全还原原命令（老 Python 的 -c / -m），不乱猜
        sys.stderr.write(
            f"[runtime] 当前解释器缺少 {miss}，且本进程以 -c/-m 方式启动、"
            f"无法安全切换解释器。\n"
            f"[runtime] 请改用：\"{target}\" 运行脚本，或先跑 bash setup.sh\n")
        return sys.executable
    os.environ[guard] = "1"
    os.execv(str(target), [str(target)] + rest)


def _reexec_argv(argv=None):
    """重入用的参数表（保留 -c / -m / 脚本参数）。

    优先 sys.orig_argv（Python 3.10+，是解释器真实收到的完整命令行）；
    老 Python 上只有 sys.argv，而 -c/-m 的源文本不在其中、无法还原，
    此时返回 None 表示"别重入"。
    """
    if argv:
        return list(argv)
    orig = getattr(sys, "orig_argv", None)
    if orig:
        rest = list(orig[1:])
        # `python -` / `python - <<EOF`：脚本正文来自 stdin，已被读走，
        # 重入后新进程读不到脚本 → 静默什么都不执行。这种情况不重入。
        if rest and rest[0] == "-":
            return None
        return rest
    if sys.argv and not str(sys.argv[0]).startswith("-"):
        return list(sys.argv)
    return None


def ensure_native_libs(argv=None, lib_dirs=("/opt/homebrew/lib", "/usr/local/lib")):
    """让 WeasyPrint 这类 cffi 扩展找到 Homebrew 的 GLib / Pango。

    macOS 的 dyld 只在**进程启动时**读一次 DYLD_FALLBACK_LIBRARY_PATH，
    进程起来后再改 os.environ 没用；所以缺这个变量时带变量重新执行一次本进程。
    只会重入一次（有环境变量守卫），装了变量就直接返回，不产生额外开销。
    """
    if os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"):
        return
    guard = f"{ENV_PREFIX}_NATIVE_LIBS"
    if os.environ.get(guard) == "1":
        return
    dirs = [d for d in lib_dirs if os.path.isdir(d)]
    if not dirs:
        return
    rest = _reexec_argv(argv)
    if rest is None:
        return
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(dirs)
    os.environ[guard] = "1"
    os.execv(sys.executable, [sys.executable] + rest)


def describe():
    lines = [f"项目：{PROJECT}",
             f"项目目录：{PROJECT_DIR}",
             f"当前解释器：{sys.executable}"]
    try:
        chosen = resolve()
        miss = missing_modules(chosen)
        lines.append(f"解析结果：{chosen}")
        lines.append("依赖状态：" + ("齐全 ✅" if not miss else f"缺少 {miss} ❌"))
    except Exception as e:
        lines.append(f"解析失败：{e}")
    lines.append("")
    lines.append("候选解释器：")
    for label, path in candidates():
        v = version_of(path)
        miss = missing_modules(path)
        ver = ".".join(map(str, v)) if v else "?"
        state = "齐全" if not miss else f"缺{len(miss)}个"
        lines.append(f"  [{state:>6}] {label:<16} {path}  (Python {ver})")
    return "\n".join(lines)


def main(argv):
    arg = argv[1] if len(argv) > 1 else ""
    if arg == "--check":
        py = resolve(strict=False)
        miss = missing_modules(py)
        print(f"解释器：{py}")
        if miss:
            print(f"缺失模块：{', '.join(miss)}")
            print(f"修复：python3 -m pip install -r {PROJECT_DIR / 'requirements.txt'}")
            return 1
        print("依赖齐全 ✅")
        return 0
    if arg == "--list":
        for label, path in candidates():
            v = version_of(path)
            miss = missing_modules(path)
            ver = ".".join(map(str, v)) if v else "?"
            print(f"{label:<16} Python {ver:<6} {path}")
            if miss:
                print(f"{'':<16} 缺：{', '.join(miss)}")
        return 0
    if arg == "--doctor":
        print(describe())
        try:
            import settings
            print()
            print(settings.describe())
        except Exception as e:      # settings 是可选的，缺了不影响解释器解析
            print(f"\n[settings] 跳过：{e}")
        return 0
    print(resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
