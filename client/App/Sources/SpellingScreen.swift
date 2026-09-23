import SwiftUI
import ERCore

/// 拼写强化（P3 决定 13，P8 §10 第 1 条补做）。
///
/// **这一套做好了一直闲置。**P3 定的规则、P5 实现进 Core（`Events.spelled`）、
/// 服务端的 `/reviews/spellings` 连离线补报都有，`spelling_available` 会在当天
/// 复习走完那一刻变 true——**而 P7 决定 21 砍掉了界面**，代价当时就写明了：
/// 「服务端那个字段会变 true 而没人读它，P3 做好的那套闲置」。
///
/// **拼错不影响调度，只记一笔**（决定 13）。所以这一屏不碰任何复习状态：
/// 它只发 `spelled` 事件，服务端把「拼成了什么」原样存下来——
/// 存的是拼成了什么，不只是对错，那才是这条记录将来的价值。
struct SpellingScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss

    let words: [SpellingWord]

    @State private var index = 0
    @State private var typed = ""
    @State private var verdict: Verdict?
    @FocusState private var focused: Bool

    enum Verdict: Equatable {
        case right
        case wrong(String)
    }

    var body: some View {
        Group {
            if index >= words.count {
                finished
            } else {
                question(words[index])
            }
        }
        .reviewBackground()
        .navigationTitle("拼写强化")
        .navigationBarTitleDisplayMode(.inline)
        // 作答是沉浸式的，不要标签栏（P12 决定 ⑪）。阅读器、作答、拼写三处一起——
        // 2026-09-23 之前一处都没写，全项目 grep 不到这个修饰符。
        .toolbar(.hidden, for: .tabBar)
    }

    private func question(_ word: SpellingWord) -> some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("\(index + 1) / \(words.count)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .monospacedDigit()

            VStack(alignment: .leading, spacing: 8) {
                Text(word.gloss.isEmpty ? "（这个词没有中文释义）" : word.gloss)
                    .font(.title3)
                if let phonetic = word.phonetic, !phonetic.isEmpty {
                    Text("/\(phonetic)/")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }

            TextField("拼出这个词", text: $typed)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                // 拼写是这个 App 唯一要打字的地方，键盘不该带联想——
                // 那等于替人把答案写出来。
                .keyboardType(.asciiCapable)
                .font(.title3)
                .focused($focused)
                .onSubmit { check(word) }
                .disabled(verdict != nil)

            if let verdict {
                switch verdict {
                case .right:
                    Label("对了", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                case .wrong(let expected):
                    VStack(alignment: .leading, spacing: 4) {
                        Label("应该是 \(expected)", systemImage: "xmark.circle.fill")
                            .foregroundStyle(.red)
                        Text("拼错不影响复习安排，只记一笔。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Spacer()

            Button(verdict == nil ? "看对不对" : "下一个") {
                if verdict == nil { check(word) } else { next() }
            }
            .buttonStyle(.borderedProminent)
            .frame(maxWidth: .infinity)
            .disabled(verdict == nil && typed.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .padding(20)
        .onAppear { focused = true }
    }

    private var finished: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.seal")
                .font(.system(size: 44))
                .foregroundStyle(.green)
            Text("拼完了")
                .font(.title3)
            Text("这一轮不进调度，只是记下来。")
                .font(.callout)
                .foregroundStyle(.secondary)
            Button("回主界面") { dismiss() }
                .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// **本地判一次，服务端也判一次**，两边用同一条规则（去空白、忽略大小写）。
    /// 本地这次只为了当场给反馈——真正记下来的是服务端算的那个结果。
    private func check(_ word: SpellingWord) {
        let answer = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !answer.isEmpty else { return }
        let correct = answer.lowercased() == word.key.lowercased()
        verdict = correct ? .right : .wrong(word.key)
        app.record(.spelled(word.key, typed: answer))
        app.log?.write(.debug, "spelling.attempted", "拼了一个词",
                       fields: ["item": word.key, "correct": correct ? "yes" : "no"])
    }

    private func next() {
        index += 1
        typed = ""
        verdict = nil
        focused = true
        if index >= words.count {
            Task { await app.drain() }
        }
    }
}
