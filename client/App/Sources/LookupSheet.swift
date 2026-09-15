import SwiftUI
import ERCore
import ERContract

/// 点词面板要显示的东西。
struct LookupItem: Equatable {
    let headword: String
    let phonetic: String?
    /// 义项集缺席时的退路：词典那一堆逗号隔开的释义。
    let fallbackGloss: String?
    let senses: [Components.Schemas.Sense]
    /// 「本句中」用的是哪个义项——**滑块标的就是它**（决定 19）。
    let contextSenseId: Int
    let isBeyondSyllabus: Bool
    var mark: MarkKind?

    var contextSense: Components.Schemas.Sense? {
        senses.first { $0.id == contextSenseId }
    }

    /// 义项排序：**按真题考频降序，考频缺失的按词典原始顺序排在后面**（决定 21）。
    ///
    /// **绝不能按义项序号排。**那个序号是模型猜的——`is_exam_key` 之所以永久为 0
    /// 就是因为它不可信。按它排等于把模型的猜测当成「常见程度」摆给学习者看。
    ///
    /// 排序在这里按数据算，所以将来义项集改了，顺序自己跟着改，不用动代码。
    static func ordered(_ senses: [Components.Schemas.Sense]) -> [Components.Schemas.Sense] {
        let withShare = senses.filter { ($0.exam?.share ?? 0) > 0 }
            .sorted { ($0.exam?.share ?? 0) > ($1.exam?.share ?? 0) }
        let withoutShare = senses.filter { ($0.exam?.share ?? 0) <= 0 }
            .sorted { $0.ordinal < $1.ordinal }
        return withShare + withoutShare
    }
}

extension Components.Schemas.Sense {
    /// 中文释义在契约里是「一串或者一个」，两种都要接得住。
    var glossText: String {
        if let list = gloss_zh?.value1, !list.isEmpty { return list.joined(separator: "，") }
        if let single = gloss_zh?.value2 { return single }
        return ""
    }

    /// 词性（`n. / vt. / vi.`）。2026-09-15 接上——`pos` 现在在契约里了。
    ///
    /// 空字符串当没有：库里那一列允许为空，而一个空的词性标签在屏幕上
    /// 就是一块无法解释的空白。
    var posText: String? {
        guard let pos, !pos.isEmpty else { return nil }
        return pos
    }

    var examShareText: String? {
        guard let share = exam?.share, share > 0 else { return nil }
        return String(format: "%.0f%%", share)
    }
}

/// 第三屏：点词面板。
///
/// 结构是固定的：上面滚，**三档滑块钉在底部不跟着滚**（决定 26）。
struct LookupSheet: View {
    let item: LookupItem
    /// 半屏，由阅读器量出来传进来。
    let maxHeight: CGFloat
    let onMark: (MarkKind?) -> Void

    @Environment(AppModel.self) private var app
    @State private var speaker = Speaker()
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    heading
                    context
                    senseList
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 12)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                    contentHeight = $0
                }
            }
            .scrollBounceBehavior(.basedOnSize)

            Divider()
            MarkSlider(mark: item.mark, onChange: onMark)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
        }
        .presentationDragIndicator(.visible)
        .presentationDetents([.height(detent)])
        // 背景可点：点下一个词要能穿过面板打到正文上（决定 25）。
        // 正文的滚动由阅读器那边关掉，一滑就收面板（决定 24）。
        .presentationBackgroundInteraction(.enabled(upThrough: .height(detent)))
        // 点词自动朗读（决定 20），**默认关**：这个 App 大概率在图书馆和地铁上
        // 用，点一下就出声会吓人一跳。`onChange` 盯着词本身——同一个面板换词
        // 时不会重建，只有 `item` 变了。
        .onAppear { if app.preferences.speakOnTap { speak() } }
        .onChange(of: item.headword) { _, _ in
            if app.preferences.speakOnTap { speak() }
        }
    }

    private func speak() {
        speaker.say(item.headword,
                    voice: app.preferences.accent.voiceCode,
                    rate: app.preferences.speechRate)
    }

    /// 按内容自适应，**最多半屏**（决定 26）。装不下的在面板里滚。
    private var detent: CGFloat {
        let slider: CGFloat = 76
        let wanted = contentHeight + slider
        let cap = maxHeight > 0 ? maxHeight : 360
        return max(220, min(wanted, cap))
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(item.headword)
                    .font(.title2.weight(.semibold))
                if item.isBeyondSyllabus {
                    // 超纲词正文里不做记号，只在这儿一行小字（决定 28）。
                    Text("超纲")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    speak()
                } label: {
                    Image(systemName: "speaker.wave.2.fill")
                        .font(.body)
                }
                .buttonStyle(.glass)
                .accessibilityLabel("朗读")
            }
            if let phonetic = item.phonetic, !phonetic.isEmpty {
                Text("/\(phonetic)/")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var context: some View {
        if let sense = item.contextSense {
            VStack(alignment: .leading, spacing: 4) {
                Text("本句中").font(.caption).foregroundStyle(.secondary)
                Text(sense.glossText).font(.body)
                if let concept = sense.concept_en, !concept.isEmpty {
                    // 英文概念定义：查词本身也是阅读输入，而不是切换到中文。
                    Text(concept).font(.callout).foregroundStyle(.secondary)
                }
            }
        } else if let fallback = item.fallbackGloss, !fallback.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("词典释义").font(.caption).foregroundStyle(.secondary)
                Text(fallback).font(.body)
            }
        }
    }

    @ViewBuilder
    private var senseList: some View {
        if !item.senses.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("常见释义 · 考频").font(.caption).foregroundStyle(.secondary)
                ForEach(item.senses, id: \.id) { sense in
                    HStack(alignment: .firstTextBaseline) {
                        Text(senseLine(sense))
                            .font(.callout)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Text(sense.examShareText ?? "—")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
            }
        }
    }

    private func senseLine(_ sense: Components.Schemas.Sense) -> String {
        guard let pos = sense.posText, !pos.isEmpty else { return sense.glossText }
        return "\(pos) \(sense.glossText)"
    }
}

/// 三档滑块：认识 / 模糊 / 不认识。
///
/// **跟多数软件不一样的地方是它可以随时来回切**，而不是「点了不认识就得再点
/// 撤销」。底下对得上系统本来的样子，不用新增任何概念：「认识」就是**没有标记**
/// 这个默认状态，滑到另外两档是标记，滑回来是撤销标记。
private struct MarkSlider: View {
    let mark: MarkKind?
    let onChange: (MarkKind?) -> Void

    private enum Slot: Hashable { case known, fuzzy, unknown }

    private var slot: Slot {
        switch mark {
        case .none: .known
        case .fuzzy: .fuzzy
        case .unknown: .unknown
        }
    }

    var body: some View {
        Picker("状态", selection: Binding(
            get: { slot },
            set: { newValue in
                switch newValue {
                case .known: onChange(nil)
                case .fuzzy: onChange(.fuzzy)
                case .unknown: onChange(.unknown)
                }
            }
        )) {
            Text("认识").tag(Slot.known)
            Text("模糊").tag(Slot.fuzzy)
            Text("不认识").tag(Slot.unknown)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }
}
