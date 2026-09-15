import SwiftUI
import ERCore

/// 第二屏：阅读器。
///
/// 这一屏最重要的决定是**什么都不标**（决定 11）：目标词、超纲词、派生词、
/// 专有名词一律不画记号，只画你自己标记过的（决定 12）。理由是满屏记号之后
/// 文章就不是文章了。Core 那条显示语义的优先级链照常算着，这里只是不用它。
struct ReaderScreen: View {
    let card: ArticleCard

    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var model = ReaderModel()
    @State private var scrolledTo: Int?
    @State private var viewportHeight: CGFloat = 0

    private static let headerID = -1

    var body: some View {
        content
            .navigationTitle(card.title)
            .navigationBarTitleDisplayMode(.large)
            .toolbar { optionsMenu }
            .task { await start() }
            .onDisappear { model.reportProgress(app: app) }
            .sheet(isPresented: lookupBinding) {
                if let item = model.lookup {
                    LookupSheet(item: item, maxHeight: viewportHeight / 2) {
                        model.setMark($0, app: app)
                    }
                }
            }
    }

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .offline(let text), .preparing(let text):
            // 转圈继续转，旁边写清楚原因（决定 33）——网一回来自己就好。
            VStack(spacing: 14) {
                ProgressView()
                Text(text).font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .task { await retryWhileOffline() }
        case .failed(let text):
            ContentUnavailableView("打不开这篇", systemImage: "exclamationmark.triangle",
                                   description: Text(text))
        case .ready:
            reader
        }
    }

    private var reader: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 20) {
                header.id(Self.headerID)

                ForEach(model.paragraphs) { paragraph in
                    ParagraphTextView(
                        paragraph: paragraph,
                        marks: model.marks,
                        selected: model.selectedSeq,
                        scale: app.preferences.fontScale,
                        onTap: { model.tap(seq: $0, app: app) }
                    )
                    .id(paragraph.id)
                }

                finishButton
            }
            .scrollTargetLayout()
            .padding(.horizontal, 20)
            .padding(.bottom, 40)
        }
        .scrollPosition(id: $scrolledTo)
        // 面板开着时正文不能滚（决定 24）。
        .scrollDisabled(model.lookup != nil)
        // 一滑就收面板。滚动此时是关着的，所以这个手势不会跟滚动打架。
        .simultaneousGesture(
            DragGesture(minimumDistance: 10).onChanged { _ in
                if model.lookup != nil { model.closeLookup() }
            }
        )
        .onChange(of: scrolledTo) { _, newValue in
            model.topParagraph = newValue
        }
        // 面板最高只能占半屏（决定 26），所以要量一下这一屏有多高——
        // `UIScreen.main` 在 iOS 26 上已经不该再用了。
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
            viewportHeight = $0
        }
        .overlay(alignment: .bottomTrailing) { backToTop }
    }

    /// 头部信息块，**跟正文一起滚走**（决定 18）。标题不在这里——它交给
    /// 导航栏的大标题，滚过之后系统自己把它缩进栏里（决定 16）。
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let summary = card.summary, !summary.isEmpty {
                Text(summary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            HStack {
                Text(card.topline)
                Spacer()
                Text(card.countsLine)
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
            Divider().padding(.top, 6)
        }
    }

    /// 「读完了」。**记账只在这一刻发生**——按下退回列表，那篇当场变已学习
    /// （决定 13）。不做收尾界面（决定 14）。
    @ViewBuilder
    private var finishButton: some View {
        if model.finished {
            Label("已学习", systemImage: "checkmark.circle.fill")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(.top, 24)
        } else {
            Button {
                model.finish(app: app)
                dismiss()
            } label: {
                Text("读完了").frame(maxWidth: .infinity)
            }
            .buttonStyle(.glassProminent)
            .controlSize(.large)
            .padding(.top, 24)
        }
    }

    /// 回到顶部（决定 15）。系统本来就有「点状态栏回到顶部」，这个是额外的、
    /// 更好发现的入口，两个并存没问题。它浮在正文上，所以它正是玻璃该出现的地方。
    private var backToTop: some View {
        Button {
            withAnimation { scrolledTo = Self.headerID }
        } label: {
            Image(systemName: "arrow.up")
                .font(.body.weight(.semibold))
                .frame(width: 44, height: 44)
        }
        .buttonStyle(.glass)
        .clipShape(.circle)
        .padding(.trailing, 18)
        .padding(.bottom, 24)
        .accessibilityLabel("回到顶部")
    }

    /// `···` 菜单。P6 决定 17 把它留成了一个说「选项还没做」的空壳，
    /// **P8 填上字号**——这一项之所以在这里而不在设置页，是因为调字号要
    /// 看着正文调：在设置页拖滑块、退出去看效果、不对再进来，
    /// 是把两秒的动作做成一分钟。
    private var optionsMenu: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Picker("正文字号", selection: Binding(
                    get: { app.preferences.fontScale },
                    set: { app.preferences.fontScale = $0 }
                )) {
                    ForEach(FontStep.all) { step in
                        Text(step.label).tag(step.value)
                    }
                }
            } label: {
                Image(systemName: "textformat.size")
            }
        }
    }

    private var lookupBinding: Binding<Bool> {
        Binding(get: { model.lookup != nil },
                set: { if !$0 { model.closeLookup() } })
    }

    private func start() async {
        guard model.phase == .loading else { return }
        await model.load(card: card, app: app)
        if case .ready = model.phase {
            scrolledTo = model.resumeParagraph ?? Self.headerID
        }
    }

    /// 离线时自己再试，不要求用户去点。转圈说的就是「还在重试」，
    /// 转着却不重试就是在骗人。
    private func retryWhileOffline() async {
        while case .offline = model.phase {
            try? await Task.sleep(for: .seconds(5))
            if Task.isCancelled { return }
            await model.load(card: card, app: app)
        }
        if case .ready = model.phase {
            scrolledTo = model.resumeParagraph ?? Self.headerID
        }
    }
}

/// 字号的几档。**离散，不给连续滑块**：在一个菜单里拖滑块很难拖准，
/// 而字号这东西本来就只有「小了／大了」两种诉求。
struct FontStep: Identifiable {
    let label: String
    let value: Double
    var id: Double { value }

    static let all: [FontStep] = [
        FontStep(label: "小", value: 0.9),
        FontStep(label: "标准", value: 1.0),
        FontStep(label: "大", value: 1.15),
        FontStep(label: "更大", value: 1.3),
        FontStep(label: "最大", value: 1.5),
    ]
}
