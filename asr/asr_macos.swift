// asr_macos.swift — macOS Speech 兜底转写（文件级，SFSpeechURLRecognitionRequest）
// qwen（百炼）是主力；本工具是 qwen 不可用时的 macOS 兜底（离线/在线均可）。
// 蒸馏自 Hermes swift_asr.swift（2026-08-26 吸收：zh-CN/dictation/标点/分段思路），
// 但改用文件级整段识别：免 stdin 切段 hack、时间戳精确到句、不占麦克风权限。
//
// 用法: asr_macos_bin <audio.wav|m4a> [--on-device] [--locale zh-CN]
// 输出（stdout，JSON 行）:
//   {"ts": <相对秒>, "end": <相对秒>, "text": "..."}   按停顿 >1s 分组的句子
//
// 编译（必须嵌入 Info.plist 供 TCC「语音识别」授权，否则 requestAuthorization 直接拒绝）:
//   swiftc -O -swift-version 5 -Xlinker -sectcreate -Xlinker __TEXT \
//          -Xlinker __info_plist -Xlinker Info.plist asr_macos.swift -o asr_macos_bin

import Foundation
import Speech

// ── 参数解析 ──
let argv = CommandLine.arguments
guard argv.count >= 2 else {
    FileHandle.standardError.write("usage: asr_macos <audio> [--on-device] [--locale zh-CN]\n".data(using: .utf8)!)
    exit(2)
}
let audioPath = argv[1]
var onDevice = false
var debug = false
var localeId = "zh-CN"
var i = 2
while i < argv.count {
    switch argv[i] {
    case "--on-device":
        onDevice = true
    case "--debug":
        debug = true
    case "--locale":
        if i + 1 < argv.count { localeId = argv[i + 1]; i += 1 }
    default:
        break
    }
    i += 1
}

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeId)), recognizer.isAvailable else {
    FileHandle.standardError.write("ASR 不可用：语言 \\(localeId) 不支持或系统语音识别不可用\n".data(using: .utf8)!)
    exit(3)
}

// ── 授权策略（2026-08-27 Hermes 适配）──
// dsh 原版显式调用 SFSpeechRecognizer.requestAuthorization：该调用触发 TCC「语音识别」
// usage-description 检查，responsible process（Hermes.app）Info.plist 无
// NSSpeechRecognitionUsageDescription → 直接 abort（"without a usage description"）。
// Hermes 原生 swift_asr.swift 从不显式授权，直接 recognitionTask → 继承 responsible
// process 隐式授权，实测正常。此处对齐：跳过 requestAuthorization，识别错误经 result
// error 回调暴露（不崩进程）。

// ── 单次文件识别（泵 RunLoop 等 final；回调在系统队列，主线程不能阻塞）──
func recognizeFile(_ url: URL, onDevice: Bool) -> (segments: [SFTranscriptionSegment], error: String?) {
    let request = SFSpeechURLRecognitionRequest(url: url)
    request.requiresOnDeviceRecognition = onDevice
    request.shouldReportPartialResults = false
    request.taskHint = .dictation
    request.addsPunctuation = true

    final class Box { var segments: [SFTranscriptionSegment] = []; var error: String? = nil; var done = false }
    let box = Box()
    _ = recognizer.recognitionTask(with: request) { result, err in
        if let result = result {
            box.segments = result.bestTranscription.segments
            if result.isFinal && !box.done { box.done = true }
        }
        if let err = err {
            if box.error == nil { box.error = err.localizedDescription }
            if !box.done { box.done = true }
        }
    }
    let deadline = Date().addingTimeInterval(900)
    while !box.done && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
    }
    return (box.segments, box.error)
}

let url = URL(fileURLWithPath: audioPath)
var (segments, err) = recognizeFile(url, onDevice: onDevice)
// 在线模式失败/为空 → 自动试本地（on-device 模型已下载时）
if (segments.isEmpty || err != nil) && !onDevice {
    let (s2, e2) = recognizeFile(url, onDevice: true)
    if !s2.isEmpty || e2 == nil { segments = s2; err = e2 }
}
guard !segments.isEmpty else {
    FileHandle.standardError.write("识别失败或结果为空\(err.map { "：\($0)" } ?? "")\n".data(using: .utf8)!)
    exit(5)
}

if debug {
    for seg in segments {
        FileHandle.standardError.write(String(format: "[seg] ts=%.2f dur=%.2f text=%@\n", seg.timestamp, seg.duration, seg.substring).data(using: .utf8)!)
    }
}

// ── 把 word 级 segments 分组为句子 ──
// macOS 会把停顿吸收进前一个 token 的 duration（实测：30s 停顿 → 「进展，」dur=30.6s），
// 且时间戳含静音（与 qwen 一致）。因此切句双触发：句末标点 + 超长 duration（≈停顿）。
var sentences: [(ts: Double, end: Double, text: String)] = []
var curTs = 0.0, curEnd = 0.0, curText = ""
let sentenceEnd = CharacterSet(charactersIn: "。！？!?；;")
for seg in segments {
    let st = seg.timestamp
    let en = st + seg.duration
    let word = seg.substring
    if curText.isEmpty {
        curTs = st
    } else if st - curEnd > 1.0 {
        sentences.append((curTs, curEnd, curText))
        curTs = st
        curText = ""
    }
    curText += word
    curEnd = en
    let endsSentence = word.rangeOfCharacter(from: sentenceEnd) != nil
    if endsSentence || seg.duration > 2.0 {
        sentences.append((curTs, curEnd, curText))
        curTs = 0
        curText = ""
    }
}
if !curText.isEmpty { sentences.append((curTs, curEnd, curText)) }

for s in sentences {
    let obj: [String: Any] = ["ts": Int(s.ts.rounded()), "end": Int(s.end.rounded()), "text": s.text]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}
