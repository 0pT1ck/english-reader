import SwiftUI
import ERCore
import ERContract

/// 一条义项在面板上的样子。单词的 `Sense` 与词组的 `PhraseSense` 统一成这一个——
/// 面板只认它，所以「词组也是一个有几条义项的东西」在画法上不用再说第二遍。
struct SenseRow: Identifiable, Equatable {
    let id: Int
    let ordinal: Int
    /// 已经取短过的中文词性（`PartOfSpeech.short`）。
    let pos: String?
    let gloss: String
    let concept: String?
    /// 占这个词（或词组）全部真题出现的百分之几。**缺席不是零**。
    let share: Double?
    /// 这条义项的用法（柯林斯加粗的搭配）。**挂在义项上**——`contribute` 三条义项
    /// 都带着 `contribute to`，意思各不相同，挂在词上这个对应关系就没了。
    let collocations: [String]

    var hasShare: Bool { (share ?? 0) > 0 }
    var shareText: String? {
        guard let share, share > 0 else { return nil }
        return String(format: "%.0f%%", share)
    }
    var line: String { pos.map { "\($0) \(gloss)" } ?? gloss }

    init(_ sense: Components.Schemas.Sense) {
        id = sense.id
        ordinal = sense.ordinal
        pos = sense.posText
        gloss = sense.glossText
        concept = sense.concept_en
        share = sense.exam?.share
        collocations = sense.collocations ?? []
    }

    init(_ sense: Components.Schemas.PhraseSense) {
        id = sense.id
        ordinal = sense.ordinal
        pos = PartOfSpeech.short(sense.pos_zh)
        gloss = sense.gloss_zh
        concept = sense.concept_en
        share = sense.exam?.share
        collocations = []
    }

    /// 义项排序：**按真题考频降序，考频缺失的按词典原始顺序排在后面**（P6 决定 21）。
    ///
    /// 排序在这里按数据算，所以将来义项集改了，顺序自己跟着改，不用动代码。
    static func ordered(_ rows: [SenseRow]) -> [SenseRow] {
        rows.filter(\.hasShare).sorted { ($0.share ?? 0) > ($1.share ?? 0) }
            + rows.filter { !$0.hasShare }.sorted { $0.ordinal < $1.ordinal }
    }
}

extension Components.Schemas.Sense {
    /// 中文释义在契约里是「一串或者一个」，两种都要接得住。
    var glossText: String {
        if let list = gloss_zh?.value1, !list.isEmpty { return list.joined(separator: "，") }
        if let single = gloss_zh?.value2 { return single }
        return ""
    }

    /// 词性，**取中文那个，而且取短**。
    ///
    /// P10 起 `pos` 装的是柯林斯的语法标记（`N-COUNT`、`ADJ-GRADED`，甚至
    /// `COLOUR` 这种根本不是词性的标签），对四六级考生是噪音；P10 §8 用「!」
    /// 标了要换，而界面这一半一直没换，2026-09-23 真机截图上还是英文缩写。
    /// **没有中文时就不显示，不退回英文**——退回去等于把这个缺陷留一半。
    /// 中文也要取短，规则在 Core 的 `PartOfSpeech.short`（`ercli` 同用）：
    /// 柯林斯给 10% 的义项挂的是一句 20 字的解释，不是一个名称。
    var posText: String? { PartOfSpeech.short(pos_zh) }
}

/// 面板上一个「可标记的东西」：一个词，或者一个词组。
struct LookupSubject: Equatable {
    enum Kind: Equatable { case word, phrase }

    let kind: Kind
    /// 标记的 `item_key`：词是 headword，词组是词组文本。
    let key: String
    /// 面板上写的那个词。平时就是 `key`；**人名照原文写**——
    /// David Green 的 Green，词形还原成 `green` 就又像那个颜色了。
    var title: String
    let phonetic: String?
    /// 已经排好序的全部义项。**专有名词这里是空的**——不给普通词的意思（决定 ⑯）。
    let senses: [SenseRow]
    /// 义项集缺席时的退路：词典那一堆逗号隔开的释义。
    let fallbackGloss: String?
    /// 从这一处标记，标记落在哪（Core 的 `MarkTarget`，决定 ⑯）。
    let target: MarkTarget
    /// 这个东西各条义项上的标记，按义项 id。**包括这一处之外的**——
    /// 列表每一行都显示自己标过没有，那就是「你标过的另一个意思」。
    var marks: [Int: MarkKind]

    var itemType: String { kind == .phrase ? "phrase" : "word" }

    var contextSenseId: Int? {
        if case .contextSense(let id) = target { return id }
        return nil
    }

    var contextSense: SenseRow? { senses.first { $0.id == contextSenseId } }

    var isMarkable: Bool {
        switch target {
        case .contextSense, .pickFromList: return true
        case .properNoun, .noSenses: return false
        }
    }

    /// 默认那一屏给哪些义项（决定 ⑫ / ⑫a）：有考频的；**一条都没有就全给**——
    /// 没有考频就没有分档的依据，那就不分。
    var primary: [SenseRow] {
        senses.contains(where: \.hasShare) ? senses.filter(\.hasShare) : senses
    }

    /// 往下才出来的：没考频的那些。只在「有的有考频、有的没有」时才有。
    var secondary: [SenseRow] {
        senses.contains(where: \.hasShare) ? senses.filter { !$0.hasShare } : []
    }
}

/// 点词面板要显示的东西。
struct LookupItem: Equatable {
    /// 滑块现在标的是哪个东西的哪条义项。
    struct Selection: Equatable {
        /// 是不是词组里那个词本身（「这个词本身」那一块里的某一行）。
        let inner: Bool
        let senseId: Int
    }

    /// 主角：点到的词，或者点到的那个词组。
    var subject: LookupSubject
    /// 词组里被点的那个词（决定 ②）。只有主角是词组时才有。
    var inner: LookupSubject?
    let isBeyondSyllabus: Bool
    /// **滑块标的是被选中的那一条。**默认是本句中那条（P6 决定 19）；
    /// 这一处没判出义项时是 nil，要人在列表里点一条（决定 ⑯）。
    var selection: Selection?

    var selectedSubject: LookupSubject? {
        guard let selection else { return nil }
        return selection.inner ? inner : subject
    }

    var selectedMark: MarkKind? {
        guard let selection, let owner = selectedSubject else { return nil }
        return owner.marks[selection.senseId]
    }
}

/// 第三屏：点词面板。P12 整个重画过（决定 ⑫、⑯）。
///
/// **两个状态**：默认那一档是「读到一半想知道这个词」用的，要短——
/// 词、音标、本句中、有考频的义项，**不带用法**；往上滑到大那一档是查词典，
/// 每条义项下面贴着它的用法，没考频的也在。展开那一档可以超过半屏（决定 ⑫c），
/// P6 决定 26 的「最多半屏」只管默认那一档。
///
/// **三档滑块钉在底部不跟着滚**（P6 决定 26）。它标的是被选中的那一行。
struct LookupSheet: View {
    let item: LookupItem
    /// 半屏，由阅读器量出来传进来。
    let maxHeight: CGFloat
    let onSelect: (LookupItem.Selection) -> Void
    let onMark: (MarkKind?) -> Void

    @Environment(AppModel.self) private var app
    @State private var speaker = Speaker()
    /// 默认那一段（到有考频的义项、以及词组的「这个词本身」那一行为止）有多高。
    @State private var defaultBlockHeight: CGFloat = 0
    @State private var expanded = false
    @State private var innerOpen = false

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 14) {
                        heading
                        context
                        rows(item.subject.primary, of: item.subject, inner: false,
                             title: item.subject.senses.contains(where: \.hasShare)
                                ? "常见释义 · 考频" : "释义")
                        if item.inner != nil { innerToggle }
                    }
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                        defaultBlockHeight = $0
                    }

                    if innerOpen, let inner = item.inner { innerSection(inner) }
                    rows(item.subject.secondary, of: item.subject, inner: false, title: "其他释义")
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 12)
            }
            .scrollBounceBehavior(.basedOnSize)

            if showsSlider {
                Divider()
                sliderArea
            }
        }
        .presentationDragIndicator(.visible)
        // **默认那一档按内容量，封顶半屏；另一档是大的**（决定 ⑫c）。
        // 量不准时（面板刚出现、内容还没排完，2026-09-15 真机上撞过）能拖大就不是问题。
        .presentationDetents([.height(detent), .large], selection: detentBinding)
        // 背景可点：点下一个词要能穿过面板打到正文上（决定 25）。**只在默认那一档**——
        // 展开之后是在查词典，那时点不到正文是认下的代价（决定 ⑫c）。
        .presentationBackgroundInteraction(.enabled(upThrough: .height(detent)))
        // 点词自动朗读（决定 20），**默认关**。`onChange` 盯着点到的东西本身——
        // 同一个面板换词时不会重建，只有 `item` 变了。
        .onAppear {
            if app.preferences.speakOnTap { speak() }
            #if DEBUG
            if DevLaunch.expandLookup { expanded = true }
            if DevLaunch.openInner { innerOpen = true }
            #endif
        }
        .onChange(of: item.subject.key) { _, _ in
            expanded = false
            innerOpen = false
            if app.preferences.speakOnTap { speak() }
        }
    }

    private var detentBinding: Binding<PresentationDetent> {
        Binding(get: { expanded ? .large : .height(detent) },
                set: { expanded = ($0 == .large) })
    }

    /// 专有名词、没有义项的词没有滑块——它们标不了（决定 ⑯）。
    private var showsSlider: Bool {
        item.subject.isMarkable || item.inner?.isMarkable == true
    }

    /// 默认那一档的高度：量到默认那一段为止，**最多半屏**（P6 决定 26）。
    private var detent: CGFloat {
        let slider: CGFloat = showsSlider ? 76 + (item.selection == nil ? 22 : 0) : 0
        let chrome: CGFloat = 18 + 12 + slider
        let cap = maxHeight > 0 ? maxHeight : 360
        return max(180, min(defaultBlockHeight + chrome, cap))
    }

    private func speak() {
        speaker.say(item.subject.key,
                    voice: app.preferences.accent.voiceCode,
                    rate: app.preferences.speechRate)
    }

    // MARK: 头部与本句中

    /// **音标跟在词后面**，不单独占一行（决定 ⑫）。「超纲」也在这一行（P6 决定 28）。
    private var heading: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(item.subject.title)
                .font(.title2.weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            if let phonetic = item.subject.phonetic, !phonetic.isEmpty {
                Text("/\(phonetic)/")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            if item.isBeyondSyllabus {
                // 超纲词正文里不做记号，只在这儿一行小字（决定 28）。
                Text("超纲").font(.caption2).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Button {
                speak()
            } label: {
                Image(systemName: "speaker.wave.2.fill").font(.body)
            }
            .buttonStyle(.glass)
            .accessibilityLabel("朗读")
        }
    }

    @ViewBuilder
    private var context: some View {
        let subject = item.subject
        switch subject.target {
        case .properNoun:
            // **人名地名不给普通词的意思**（决定 ⑯）。起因是 David Green 的 Green：
            // 面板给了颜色 green 的「绿色的，未成熟的」，还让人标成了不认识。
            VStack(alignment: .leading, spacing: 4) {
                Text("专有名词").font(.caption).foregroundStyle(.secondary)
                Text("人名、地名这类。不给普通词的意思，也不标记。")
                    .font(.callout).foregroundStyle(.secondary)
            }
        case .noSenses:
            VStack(alignment: .leading, spacing: 4) {
                Text("词典释义").font(.caption).foregroundStyle(.secondary)
                if let fallback = subject.fallbackGloss, !fallback.isEmpty {
                    Text(fallback).font(.body)
                }
                Text("这个词还没有义项，标了进不了复习，所以这里不给标。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        case .contextSense:
            if let sense = subject.contextSense {
                VStack(alignment: .leading, spacing: 4) {
                    Text("本句中").font(.caption).foregroundStyle(.secondary)
                    Text(sense.gloss).font(.body)
                    if let concept = sense.concept, !concept.isEmpty {
                        // 英文定义：查词本身也是阅读输入，而不是切换到中文。
                        Text(concept).font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
        case .pickFromList:
            // 这一处没判出是哪个意思（功能词从不标注、标注判「都不贴合」）。
            // 给词典那一堆当参考，标记要人在下面挑（决定 ⑯）。
            VStack(alignment: .leading, spacing: 4) {
                Text("词典释义").font(.caption).foregroundStyle(.secondary)
                if let fallback = subject.fallbackGloss, !fallback.isEmpty {
                    Text(fallback).font(.body)
                }
            }
        }
    }

    // MARK: 义项列表

    @ViewBuilder
    private func rows(_ rows: [SenseRow], of owner: LookupSubject, inner: Bool,
                      title: String) -> some View {
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption).foregroundStyle(.secondary)
                    .padding(.bottom, 4)
                ForEach(rows) { row in
                    senseRow(row, of: owner, inner: inner)
                }
            }
        }
    }

    /// 一行义项。**能点就是在选「滑块标哪一条」**（决定 ⑯）；
    /// 右边写着这一条标过没有——那就是「你标过的另一个意思」。
    private func senseRow(_ row: SenseRow, of owner: LookupSubject, inner: Bool) -> some View {
        let selected = item.selection == .init(inner: inner, senseId: row.id)
        return Button {
            onSelect(.init(inner: inner, senseId: row.id))
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(row.line)
                        .font(.callout)
                        .foregroundStyle(.primary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if let mark = owner.marks[row.id] {
                        Text(mark == .unknown ? "不认识" : "模糊")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    // 没考频的那一段不画考频列（决定 ⑫）：一列占着地方只为说「没有」。
                    if let share = row.shareText {
                        Text(share)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
                // **用法只在展开之后出现，贴在它那条义项下面**（决定 ⑫b）。
                if expanded, !row.collocations.isEmpty {
                    Text(row.collocations.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 5)
            .padding(.horizontal, 8)
            .background(selected ? Color.secondary.opacity(0.14) : .clear,
                        in: RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!owner.isMarkable)
        .padding(.horizontal, -8)
    }

    // MARK: 词组里的那个词（决定 ②，B）

    /// 「这个词本身」那一行。**在其他释义上面**（决定 ⑭），所以它算进默认那一屏。
    private var innerToggle: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { innerOpen.toggle() }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: innerOpen ? "chevron.down" : "chevron.right")
                    .font(.caption.weight(.semibold))
                Text("\(item.inner?.key ?? "") 这个词本身").font(.callout)
                Spacer()
            }
            .foregroundStyle(.secondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// 展开之后是那个词完整的面板——**只是不画「本句中」**：它自己的标注是在
    /// 不知道自己在词组里的情况下做的，显示一个已知可能错的高亮比不显示更糟（决定 ②）。
    @ViewBuilder
    private func innerSection(_ inner: LookupSubject) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(inner.key).font(.headline)
                if let phonetic = inner.phonetic, !phonetic.isEmpty {
                    Text("/\(phonetic)/").font(.subheadline).foregroundStyle(.secondary)
                }
            }
            if inner.senses.isEmpty {
                if let fallback = inner.fallbackGloss, !fallback.isEmpty {
                    Text(fallback).font(.callout)
                }
            } else {
                rows(inner.senses, of: inner, inner: true,
                     title: inner.senses.contains(where: \.hasShare) ? "释义 · 考频" : "释义")
            }
        }
        .padding(.leading, 12)
    }

    // MARK: 滑块

    @ViewBuilder
    private var sliderArea: some View {
        VStack(spacing: 6) {
            if item.selection == nil {
                Text("点上面的一条义项，标记就落在那一条上")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            MarkSlider(mark: item.selectedMark, onChange: onMark)
                .disabled(item.selection == nil)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
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
