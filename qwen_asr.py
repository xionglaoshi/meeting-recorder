"""qwen-audio-3.0-asr-flash-streaming 客户端（原生 WebSocket run-task 协议）

对比 paraformer 的优势：
- 新一代模型（2026-07-31 发布，中文工业场景错字率 7.8%，Artificial Analysis 1.7% 错字率第一）
- 支持上下文增强（context 注入领域术语 → 显著提升专有词准确率）
- 支持热词（2000 上限，paraformer 500）
- 语音润色、直接输出结构化文本

协议：WebSocket run-task（duplex）
- 连接 → run-task JSON → task-started → 二进制 PCM 流 → finish-task → 结果流 → task-finished
"""
import base64
import json
import os
import threading
import time
import uuid

import websocket

# 流式 ASR 的 WebSocket 端点：默认官方公共端点，
# 用百炼"专属部署"的人在 .env 里配 $DASHSCOPE_BASE_URL（见 settings.py）
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import settings as _settings

WS_URL = _settings.dashscope_ws_url()
SAMPLE_RATE = 16000
CHUNK = 3200  # 100ms @16k mono


class QwenASRClient:
    """qwen-audio-3.0-asr-flash-streaming 客户端（单任务生命周期）"""

    def __init__(self, api_key, context="", hotwords=None, model="qwen-audio-3.0-asr-flash-streaming"):
        self.api_key = api_key
        self.context = context          # 上下文增强文本（领域术语）
        self.hotwords = hotwords or []  # 热词列表
        self.model = model
        self._ws = None
        self._task_id = uuid.uuid4().hex[:32]
        self._sentences = []            # 完成句 [{text, start_ms, end_ms}]
        self._partial = ""              # 当前增量
        self._done = threading.Event()
        self._error = ""
        self._started = threading.Event()

    # ── 事件回调（子类可覆盖）──────────
    def on_sentence(self, text, start_ms, end_ms):
        """完整句子回调"""
        pass

    def on_partial(self, text):
        """增量（未完成句）回调"""
        pass

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return
        header = data.get("header", {})
        event = header.get("event", "")
        payload = data.get("payload", {})

        if event == "task-started":
            self._started.set()
        elif event == "task-finished":
            self._done.set()
        elif event == "task-failed":
            self._error = header.get("error_message", "") or json.dumps(data, ensure_ascii=False)[:300]
            print(f"[qwen_asr] 任务失败: {self._error}", flush=True)
            self._done.set()
        elif event in ("result", "result-generated"):
            # 结果事件：payload.output.sentence 结构（增量流，end_time 非 null 表示句子完成）
            try:
                sentence = payload.get("output", {}).get("sentence", {})
                text = sentence.get("text", "") or ""
                begin = sentence.get("begin_time", 0) or 0
                end = sentence.get("end_time")
                if not text:
                    return
                if end is not None:
                    # 完整句
                    self._sentences.append({"text": text, "start_ms": begin, "end_ms": end})
                    self._partial = ""
                    self.on_sentence(text, begin, end)
                else:
                    # 增量：直接回调完整当前文本（engine 侧做跨流去重，避免多流重叠）
                    self._partial = text
                    self.on_partial(text)
            except Exception as e:
                print(f"[qwen_asr] 结果解析失败: {e}", flush=True)

    def _on_error(self, ws, error):
        self._error = str(error)
        print(f"[qwen_asr] 连接错误: {error}", flush=True)
        self._done.set()

    def _on_open(self, ws):
        # run-task（带上下文 + 即时热词）
        params = {"format": "pcm", "sample_rate": SAMPLE_RATE}
        if self.hotwords:
            # 即时热词：对象 {热词: 权重}（官方格式，qwen-audio 专用，最多 2000）
            vocab_obj = {}
            for w in self.hotwords[:1200]:
                # 高价值词（人名/简称，≤4字）weight 5；其余 4
                vocab_obj[w] = 5 if len(w) <= 4 else 4
            params["vocabulary"] = vocab_obj
        run_task = {
            "header": {"action": "run-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": self.model,
                "parameters": params,
                "input": {},
            },
        }
        if self.context:
            run_task["payload"]["input"]["context"] = [
                {"role": "user", "content": [{"type": "input_text", "text": self.context[:4000]}]}
            ]
        ws.send(json.dumps(run_task))
        print(f"[qwen_asr] run-task 已发送（model={self.model}, 上下文 {len(self.context)} 字, 热词 {len(self.hotwords)}）", flush=True)

    # ── 对外接口 ──────────────────────
    def start(self):
        """建立连接并发送 run-task（异步，等 task-started）"""
        self._ws = websocket.WebSocketApp(
            WS_URL,
            header={"Authorization": f"Bearer {self.api_key}"},
            on_message=self._on_message,
            on_error=self._on_error,
            on_open=self._on_open,
        )
        threading.Thread(target=self._ws.run_forever, daemon=True).start()
        self._started.wait(timeout=15)
        return self._started.is_set()

    def send_audio(self, chunk: bytes):
        """发送 PCM 音频块（16k mono s16le）"""
        if self._ws and self._started.is_set():
            try:
                self._ws.send_bytes(chunk)
                self._error = ""   # 2026-08-27：发送成功清空历史错误（供 engine 连续失败计数）
                return True
            except Exception as e:
                self._error = f"发送失败: {e}"
                return False
        return False

    def finish(self):
        """结束任务：发 finish-task，等结果"""
        if self._ws:
            try:
                self._ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": self._task_id, "streaming": "duplex"},
                    "payload": {"input": {}},
                }))
            except Exception:
                pass
        self._done.wait(timeout=20)

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def sentences(self):
        return self._sentences

    @property
    def error(self):
        return self._error


def build_context_from_vocab(vocab_path):
    """从字典生成上下文增强文本（person/company/project/partner 全量）。
    person 优先（人名是纠错重点，qwen 上下文对名字纠正最有效）。"""
    import json
    try:
        with open(vocab_path, encoding="utf-8") as f:
            v = json.load(f)
        cats = v.get("categories", {})
        terms = []
        for cat in ("person", "company", "project", "partner", "business", "industry"):
            for it in cats.get(cat, []):
                term = it.get("term", "")
                if term and 2 <= len(term) <= 15:
                    terms.append(term)
                for var in (it.get("variants") or [])[:3]:
                    if var and 2 <= len(var) <= 15:
                        terms.append(var)
        # 去重 + 截断（上下文有限，人名优先保前 150，其余补充）
        seen = []
        for t in terms:
            if t not in seen:
                seen.append(t)
        return "、".join(seen[:300])
    except Exception:
        return ""


if __name__ == "__main__":
    # 单测：识别测试音频
    import subprocess
    import sys
    sys.path.insert(0, ".")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from runtime import ensure_runtime
    ensure_runtime()
    import settings
    key = settings.require("DASHSCOPE_API_KEY")
    ctx = build_context_from_vocab("vocab.json")
    client = QwenASRClient(key, context=ctx)
    if not client.start():
        print("❌ 连接失败"); sys.exit(1)
    # 读 m4a → pcm
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", "/tmp/test_asr.m4a", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True)
    pcm = p.stdout
    for i in range(0, len(pcm), CHUNK):
        client.send_audio(pcm[i:i + CHUNK])
        time.sleep(0.03)
    client.finish()
    print("\n=== qwen-audio 结果 ===")
    for s in client.sentences:
        print(f"  {s['text']}")
    if not client.sentences:
        print(f"  （无结果，error={client.error}）")
    client.close()
