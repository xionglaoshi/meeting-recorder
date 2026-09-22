// swift_asr.swift — macOS Speech 框架分段实时中文转写
//
// 用法：swift swift_asr.swift < 16k_mono_pcm (stdin 持续流入)
// 输出：识别到内容即向 stdout 打一行 JSON：
//   {"ts":"HH:MM:SS","text":"...","final":true}
//
// 分段策略：Speech 框架在管道喂入模式下结果要 EOF 才 flush，因此每累计
// SEGMENT_BYTES（约 10s 音频）调一次 request.endAudio() 强制出 final，
// 然后重建 recognitionTask 继续 —— 准实时（约 10s 延迟一段）。
// 主循环同步驱动分段，无定时器、无竞态。
//
// 输入：16kHz mono s16le PCM（与 ffmpeg 输出一致）

import Foundation
import Speech
import AVFoundation

let SAMPLE_RATE = 16000
let CHUNK_BYTES = 3200          // 100ms @16k mono s16
let SEGMENT_BYTES = 160000      // 约 10 秒音频 (16000*2*10)

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN")),
      recognizer.isAvailable else {
    FileHandle.standardError.write("ASR不可用".data(using: .utf8)!)
    exit(1)
}

print("READY")
fflush(stdout)

// 累计音频秒数（相对会议开始，供流水时间段用）
var consumedBytes = 0

func emitSegment(_ text: String) {
    let relSec = consumedBytes / (SAMPLE_RATE * 2)   // 段起始相对秒
    let json: [String: Any] = ["ts": relSec, "text": text, "final": true]
    if let data = try? JSONSerialization.data(withJSONObject: json),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

// 当前段状态
var request = SFSpeechAudioBufferRecognitionRequest()
var task: SFSpeechRecognitionTask? = nil
var segmentBytes = 0

func startSegment() {
    request = SFSpeechAudioBufferRecognitionRequest()
    request.shouldReportPartialResults = true
    request.taskHint = .dictation
    request.addsPunctuation = true
    segmentBytes = 0

    task = recognizer.recognitionTask(with: request) { result, error in
        if let result = result, result.isFinal {
            let text = result.bestTranscription.formattedString
            if !text.isEmpty {
                emitSegment(text)
            }
        }
        if let error = error {
            FileHandle.standardError.write("识别错误: \(error.localizedDescription)\n".data(using: .utf8)!)
        }
    }
}

func feed(_ chunk: Data) {
    let samples = chunk.withUnsafeBytes { raw -> [Float] in
        let count = raw.count / 2
        guard count > 0 else { return [] }
        var out = [Float](repeating: 0, count: count)
        raw.bindMemory(to: Int16.self).baseAddress.map { base in
            for i in 0..<count {
                out[i] = Float(base[i]) / 32768.0
            }
        }
        return out
    }
    guard !samples.isEmpty else { return }
    guard let format = AVAudioFormat(standardFormatWithSampleRate: Double(SAMPLE_RATE), channels: 1),
          let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { return }
    pcm.frameLength = AVAudioFrameCount(samples.count)
    samples.withUnsafeBufferPointer { ptr in
        pcm.floatChannelData![0].update(from: ptr.baseAddress!, count: samples.count)
    }
    request.append(pcm)
    segmentBytes += chunk.count
    consumedBytes += chunk.count

    // 累计满一段 → 切段（endAudio 出 final → 重建）
    if segmentBytes >= SEGMENT_BYTES {
        request.endAudio()
        // 给 final 回调一点时间
        let deadline = Date().addingTimeInterval(2.0)
        while Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        startSegment()
    }
}

// ── 主循环 ──────────────────────────────────────────────
startSegment()
let input = FileHandle.standardInput
let chunkSize = CHUNK_BYTES
var done = false

while !done {
    guard let data = try? input.read(upToCount: chunkSize) else { break }
    if data.isEmpty {
        done = true
        break
    }
    feed(data)
}

// EOF → 结束最后一段
request.endAudio()
let deadline = Date().addingTimeInterval(5)
while Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.2))
}
exit(0)
