import SwiftUI
import ERCore
import ERContract

/// 第二、三屏：作答与揭晓，外加做完那一屏。
///
/// 三个状态走一条直线：题面 →（可选）提示 → 揭晓 → 下一题。
/// **作答在按「下一个」时才落盘**，因为揭晓屏上还能改主意（决定 13）。
struct ReviewSessionScreen: View {
    @Bindable var model: ReviewModel
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingDismiss = false

    var body: some View {
        Group {
            if model.card == nil {
                if model.allDone {
                    DoneView { dismiss() }
                } else {
                    // 这个池子做完了，另一个还欠着——直接退回主界面。
                    Color.clear.onAppear { dismiss() }
                }
            } else {
                question
            }
        }
        .reviewBackground()
        .navigationTitle("复习")
        .navigationBarTitleDisplayMode(.inline)
        // 作答是沉浸式的，不要标签栏（P12 决定 ⑪，三处一起，见 `SpellingScreen`）。
        .toolbar(.hidden, for: .tabBar)
        .confirmationDialog(model.card?.item_type == "phrase" ? "不再复习这个词组？" : "不再复习这个词？",
                            isPresented: $confirmingDismiss,
                            titleVisibility: .visible) {
            Button("我已经会了", role: .destructive) { model.dismissCurrent(app) }
            Button("算了", role: .cancel) {}
        } message: {
            // 撤销标记之后条目退回 `new`，但遇见记录保留——它确实被遇见过。
            // 这句话要说出来，因为「会了」和「删掉它」在人心里是两回事。
            Text("它会离开复习队列，退回「没学过」那一档。"
                 + "你读过它、在哪篇读到的，都还留着；以后再标一次它还会回来。")
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    // **「太简单了」在这儿，不在作答那一排按钮里**（2026-09-17 用户定）：
                    // 它要的是「我确实是特意点它的」，而三个并排的按钮做不到。
                    // 在这之前这个声明只有命令行客户端发得出来，
                    // 而 P7 定的「提示要付代价」那条规则因此没有任何界面能触发。
                    Button("太简单了", systemImage: "hare") { model.claimEasy() }
                        .disabled(!model.canClaimEasy)
                    if model.claimedEasy {
                        // 点过了要看得出来，否则它和「点了没反应」长得一样。
                        Label("这一轮按「太简单」算", systemImage: "checkmark")
                    } else if model.easyWithdrawn {
                        // 灰着要说得出为什么。磕过之后说「太简单」不是关于任何
                        // 事情的断言，Core 与服务端都会当场撤回这个声明。
                        Label("这一轮磕过了，算不了简单", systemImage: "info.circle")
                    }

                    Divider()

                    // P7 决定 22 留的空壳，P8 填上。代价当时写得很清楚：
                    // **词一旦标了，只能等 FSRS 慢慢放过它**——而这条出口
                    // 整条路本来就是通的，缺的只是这个菜单项。
                    Button(model.card?.item_type == "phrase" ? "这个词组我已经会了" : "这个词我已经会了",
                           systemImage: "checkmark.circle") {
                        confirmingDismiss = true
                    }
                    .disabled(model.card == nil)
                } label: {
                    Image(systemName: "ellipsis")
                }
            }
        }
    }

    private var question: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    HighlightedText(text: model.prompt, highlight: model.promptHighlight)
                        .font(.title3)
                        .lineSpacing(6)

                    if let hint = model.hint {
                        HintBlock(hint: hint)
                    }

                    if case .revealed = model.step {
                        reveal
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 24)
                .padding(.bottom, 20)
            }

            Divider()
            buttons.padding(.horizontal, 16).padding(.vertical, 12)
        }
    }

    /// 揭晓：另一个方向的句子 + 本句中 + 常见释义·考频。
    ///
    /// **答对了也给看。**纯自评没有纠错机会，而复习的价值恰恰在看到正确答案那一下。
    @ViewBuilder
    private var reveal: some View {
        if let card = model.card {
            VStack(alignment: .leading, spacing: 20) {
                if let other = otherSide {
                    HighlightedText(text: other.text, highlight: other.range)
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .lineSpacing(4)
                }

                if let sense = card.sense {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("本句中").font(.caption).foregroundStyle(.secondary)
                        Text(HintLadder.chineseGloss(card)).font(.body)
                        if let concept = sense.concept_en, !concept.isEmpty {
                            Text(concept).font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }

                if let word = card.word, let senses = word.senses, !senses.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            // 一条考频都没有时不写「考频」（同点词面板，P12 决定 ⑫a）。
                            Text(senses.contains { ($0.share ?? 0) > 0 } ? "常见释义 · 考频" : "释义")
                                .font(.caption).foregroundStyle(.secondary)
                            if let phonetic = word.phonetic, !phonetic.isEmpty {
                                Text("/\(phonetic)/").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        // **按考频降序，绝不按义项序号**——那个序号是模型猜的。
                        ForEach(senses.sorted { ($0.share ?? -1) > ($1.share ?? -1) },
                                id: \.id) { sense in
                            HStack(alignment: .firstTextBaseline) {
                                Text(line(sense))
                                    .font(.callout)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                // 没考频的不画「—」（P12 决定 ⑫，跟点词面板同一条）：
                                // 一列占着地方只为说「没有」。
                                if let share = sense.share, share > 0 {
                                    Text(String(format: "%.0f%%", share))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .monospacedDigit()
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /// 揭晓时补上的另一面：看词想义补中文整句，看中文想英文补英文原句。
    private var otherSide: (text: String, range: Range<Int>?)? {
        guard let asked = model.askedCard else { return nil }
        switch model.direction {
        case .wordToSense:
            guard let zh = asked.text_zh, !zh.isEmpty else { return nil }
            return (zh, HintLadder.promptHighlight(asked, direction: .senseToWord))
        case .senseToWord:
            let start = max(0, min(asked.blank_start, asked.text.count))
            let end = max(start, min(asked.blank_end, asked.text.count))
            return (asked.text, start < end ? start..<end : nil)
        }
    }

    /// 词性取中文、取短（P12 决定 ⑦）：原先取的是 `pos`，揭晓屏上显示 `N-COUNT`。
    /// 契约 3 起 `WordSense` 才有 `pos_zh`——点词面板那边早就有，这边一直没有。
    private func line(_ sense: Components.Schemas.WordSense) -> String {
        let gloss: String
        if let list = sense.gloss_zh?.value1 { gloss = list.joined(separator: ", ") }
        else { gloss = sense.gloss_zh?.value2 ?? "" }
        guard let pos = PartOfSpeech.short(sense.pos_zh) else { return gloss }
        return "\(pos) \(gloss)"
    }

    @ViewBuilder
    private var buttons: some View {
        switch model.step {
        case .asking:
            HStack(spacing: 12) {
                Button("认识") { model.answer(passed: true) }
                    .buttonStyle(.glass)
                    .frame(maxWidth: .infinity)
                if model.hintAvailable {
                    Button("不确定") { model.reveal() }
                        .buttonStyle(.glass)
                        .frame(maxWidth: .infinity)
                }
                Button("不认识") { model.answer(passed: false) }
                    .buttonStyle(.glass)
                    .frame(maxWidth: .infinity)
            }
        case .hinted:
            // 提示一级到底，所以按钮从三个变成两个（决定 16）。
            HStack(spacing: 12) {
                Button("认识") { model.answer(passed: true) }
                    .buttonStyle(.glass)
                    .frame(maxWidth: .infinity)
                Button("不认识") { model.answer(passed: false) }
                    .buttonStyle(.glass)
                    .frame(maxWidth: .infinity)
            }
        case .revealed:
            HStack(spacing: 12) {
                // 防误触：想点认识结果点成不认识，在这儿能划回去（决定 12）。
                Picker("", selection: Binding(
                    get: { model.answeredPassed },
                    set: { model.amend(passed: $0) }
                )) {
                    Text("认识").tag(true)
                    Text("不认识").tag(false)
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                Button("下一个") { model.advance(app) }
                    .buttonStyle(.glassProminent)
            }
        }
    }
}

/// 一段文字，中间某一段高亮。
///
/// 按**字符下标**切，不是按字符串搜：同一个词在句子里可能出现两次，搜会命中错的
/// 那一个——服务端给区间正是为了这件事。
private struct HighlightedText: View {
    let text: String
    let highlight: Range<Int>?

    var body: some View {
        if let range = highlight, range.upperBound <= text.count, !range.isEmpty {
            let chars = Array(text)
            (Text(String(chars[0..<range.lowerBound]))
             + Text(String(chars[range.lowerBound..<range.upperBound]))
                .bold().foregroundStyle(Color.accentColor)
             + Text(String(chars[range.upperBound...])))
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            Text(text).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// 提示：原句 + 出处（方向 2 的原句是挖空的，还带首字母）。
private struct HintBlock: View {
    let hint: Hint

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(hint.body)
                .font(.callout.weight(.medium))
                .foregroundStyle(.orange)
                .lineSpacing(3)
            Text(hint.label)
                .font(.caption)
                .foregroundStyle(.orange)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}

/// 做完了（决定 20）。**两张卡都做完才显示这一屏**——只做完一张就说
/// 「今天的所有复习」是句假话，那时直接回主界面，反馈是那张卡变绿。
private struct DoneView: View {
    let back: () -> Void

    var body: some View {
        VStack {
            Spacer()
            Text("恭喜！\n您已完成今天的所有复习！")
                .font(.title2.weight(.semibold))
                .multilineTextAlignment(.center)
            Spacer()
            Button("返回主菜单", action: back)
                .buttonStyle(.glassProminent)
                .controlSize(.large)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
        }
    }
}
