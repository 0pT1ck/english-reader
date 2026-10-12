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
    /// 正文里那个大标题有多高，以及它是不是已经滚出去了——栏里的小标题
    /// 看这个决定出不出现（决定 ⑩ 的 C）。
    @State private var titleHeight: CGFloat = 0
    @State private var titleScrolledAway = false
    /// 打开时要对准的那一段（上次读到的位置），**在用户自己开始滑之前**反复对准。
    @State private var pendingAnchor: Anchor?
    /// 一次「现在就滚过去」的请求——文内搜索跳转用。排版早就量完了，
    /// 等不到「内容长高」那个时机，所以要一个直接的触发。
    @State private var jumpRequest: Anchor?
    /// 文内搜索开着没有（P12 决定 ㉑）。
    @State private var searching = false

    private static let headerID = -1

    var body: some View {
        content
            // **标题自己画，栏里那个小标题自己管**（P12 决定 ⑩，方案 §7 三的 C）。
            //
            // 原来交给导航栏的大标题（P6 决定 16），为的是「往上滑，大标题缩进栏里
            // 固定住」那个系统动画。而 iOS 的大标题是单行截断的，长标题只剩
            // 「A New Life for an Old…」。先试的 B1（放开那个 label 的行数）
            // 2026-09-23 在模拟器上对照过：折行了，但系统给那一块的高度是写死的
            // 一行，第二行被裁掉、省略号也没了——看起来像标题本来就这么长，
            // 比截断更糟。所以转 C：正文顶上画完整的标题，滚出去之后
            // 栏里的小标题淡进来。`navigationTitle` 仍然设着，
            // 返回按钮长按的菜单和旁白要用它；它不在栏里显示，因为 `.principal` 占了那个位置。
            .navigationTitle(card.title)
            .navigationBarTitleDisplayMode(.inline)
            // 读文章是沉浸式的，不要标签栏（P12 决定 ⑪）。原先它一直浮在正文上，
            // 最后一行被那个玻璃胶囊压掉半行。三处一起，见 `SpellingScreen`。
            .toolbar(.hidden, for: .tabBar)
            .toolbar {
                optionsMenu
                ToolbarItem(placement: .principal) {
                    Text(card.title)
                        .font(.headline)
                        .lineLimit(1)
                        .opacity(titleScrolledAway ? 1 : 0)
                        .animation(.easeInOut(duration: 0.2), value: titleScrolledAway)
                }
            }
            .task { await start() }
            .onDisappear {
                model.reportProgress(app: app)
                // **记完就发。**读到哪一段存在服务端（`progress.sentence_seq`），
                // 而事件只是落进了发件箱——不发出去的话，下次打开这篇文章
                // 服务端还以为你没读过，于是回到第一段。
                // 退出一篇文章是个自然的同步点，而且就一条事件。
                Task { await app.drain() }
            }
            // 文内搜索（决定 ㉑）。**跟点词面板挂在同一个视图上**——挂在里层的滚动视图上时，
            // 2026-09-23 模拟器上它根本弹不出来（外层已经挂着一个 sheet）。
            .sheet(isPresented: $searching) {
                ArticleSearchSheet(model: model, initialQuery: devSearchQuery) { hit in
                    searching = false
                    jump(to: hit)
                }
            }
            .sheet(isPresented: lookupBinding) {
                if let item = model.lookup {
                    LookupSheet(item: item, maxHeight: viewportHeight / 2,
                                onSelect: { model.select($0) },
                                onMark: { model.setMark($0, app: app) })
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
        ScrollViewReader { proxy in
        ScrollView {
            // **VStack 而不是 LazyVStack。**每段是一个 `UITextView`，而懒加载
            // 意味着它们在滚动到跟前时才测量——于是恢复上次位置那一下，
            // 十几段边测边排，真机上看到的就是「整体错位，几秒后归位」。
            // 一篇文章十几段，一次全渲染的代价远小于那几秒的抖动。
            VStack(alignment: .leading, spacing: 20) {
                header.id(Self.headerID)

                ForEach(model.paragraphs) { paragraph in
                    ParagraphTextView(
                        paragraph: paragraph,
                        marks: model.markSpans,
                        selected: model.selectedSpan,
                        scale: app.preferences.fontScale,
                        onTap: { model.tap(seq: $0, app: app) }
                    )
                    .id(paragraph.id)
                }

                finishButton
            }
            .scrollTargetLayout()
            .padding(.horizontal, 20)
            // 底部留白。**「回到顶部」那个浮钮 P12 删了**（决定 ⑲），这里原先按它的尺寸
            // 算（标签栏藏掉之后「读完了」曾被它压住）；它不在了，就回到一个普通的留白。
            // 系统自带的「点状态栏回到顶部」照样能用。
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
        // 大标题整个滚到栏底下之后，栏里的小标题才出来——一半在屏上时两个同时出现，
        // 就是同一句话说两遍。`contentInsets.top` 要加上：内容是从栏下面开始排的。
        .onScrollGeometryChange(for: Bool.self) { geometry in
            titleHeight > 0 && geometry.contentOffset.y + geometry.contentInsets.top > titleHeight
        } action: { _, away in
            titleScrolledAway = away
        }
        // 面板最高只能占半屏（决定 26），所以要量一下这一屏有多高——
        // `UIScreen.main` 在 iOS 26 上已经不该再用了。
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
            viewportHeight = $0
        }
        // **跳回上次位置（决定 15）要等排版量完才对得准。**打开的那一刻每一段的
        // 高度都还没量出来（正文每段是一个 `UITextView`，高度事后才报上来），
        // 那时设的位置是按全是零的高度算的，结果停在顶部。2026-09-23 在 iOS 27
        // 模拟器上对照过：原版代码存着第 11 句，打开是顶部——P9 的注释记过
        // iOS 26 真机上「整体错位，几秒后归位」，到 27 上干脆不归位了。
        // 所以内容每长高一次就再对准一次，**直到用户自己动手滑、或者改了字号**为止——
        // 不猜一个「等多少毫秒」：量完要多久随篇幅和机器变，猜的数总有一天不够。
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentSize.height } action: { _, _ in
            guard let target = pendingAnchor else { return }
            proxy.scrollTo(target.paragraph, anchor: target.point)
        }
        .onChange(of: jumpRequest) { _, request in
            guard let request else { return }
            withAnimation { proxy.scrollTo(request.paragraph, anchor: request.point) }
        }
        .onScrollPhaseChange { _, phase in
            if phase == .interacting { pendingAnchor = nil }
        }
        .onChange(of: app.preferences.fontScale) { _, _ in pendingAnchor = nil }
        }
    }

    /// 头部信息块，**跟正文一起滚走**（决定 18）。
    ///
    /// **标题在这里**（P12 起），想占几行占几行——原先交给导航栏的大标题，
    /// 而那个是单行截断的。字号用 `.largeTitle` 加粗，就是系统大标题那一档。
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(card.title)
                .font(.largeTitle.bold())
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                    titleHeight = $0
                }
                .padding(.bottom, 4)
            if let summary = card.summary, !summary.isEmpty {
                // **左对齐**（P12 决定 ⑨）。P6 的草图上它是右对齐的——草图上那句只有一行，
                // 一行右对齐是条干净的线；两行右对齐左边就参差了，中文没有词间空隙
                // 吸收那种参差。下面「话题 · 词数」那一行照旧左右分居。
                Text(summary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
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

    /// 右上角的「选项」（P12 决定 ⑳）。原先那里直接是字号按钮（P8 决定 17）；
    /// 用户 2026-09-23 定成一个选项菜单，**字号收进二级菜单**，旁边加「搜索单词」。
    /// 字号仍然在阅读屏而不在设置页——调字号要看着正文调（P8 的理由不变）。
    private var optionsMenu: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button("在这篇里找单词", systemImage: "magnifyingglass") {
                    model.closeLookup()
                    searching = true
                }
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
                    Label("字号", systemImage: "textformat.size")
                }
            } label: {
                Image(systemName: "ellipsis")
            }
            .accessibilityLabel("选项")
        }
    }

    /// 从搜索结果跳过去：滚到那一处，给它加底色（决定 ㉑）。
    ///
    /// **按「它在这一段里的相对位置」对准，不按段首**。`scrollTo(id, anchor: y)` 把这一段
    /// 高度 y 处的那一点放到屏幕高度 y 处——y 取「那个词在这段里走到了几成」，
    /// 那个词就一定落在屏上，不管这段多长、字号多大。按段首对准的话，
    /// 2026-09-23 量过：生成文最长一段 716 字符、一屏放得下；**真题有 17 段超过一屏**
    /// （最长 1,521 字符），字号拧大之后更多——那个词会落在屏幕下面。
    /// 按字符比例估位置不是精确的行坐标（一行的字数不齐），但误差在一两行之内，
    /// 而量精确坐标要伸手进 `UITextView` 的排版，为这点精度不值得。
    private func jump(to hit: ReaderModel.SearchHit) {
        model.highlight(hit.seqs)
        guard let paragraph = model.paragraphs.first(where: { $0.id == hit.paragraph }),
              let token = paragraph.tokens.first(where: { $0.seq == hit.seqs.lowerBound }) else {
            return
        }
        let length = max(1, (paragraph.text as NSString).length)
        let fraction = min(1, max(0, (Double(token.location) + Double(token.length) / 2)
                                      / Double(length)))
        let target = Anchor(paragraph: hit.paragraph, point: UnitPoint(x: 0.5, y: fraction))
        scrolledTo = hit.paragraph
        pendingAnchor = target
        jumpRequest = target
    }

    private var devSearchQuery: String {
        #if DEBUG
        return DevLaunch.searchQuery ?? ""
        #else
        return ""
        #endif
    }

    private var lookupBinding: Binding<Bool> {
        Binding(get: { model.lookup != nil },
                set: { if !$0 { model.closeLookup() } })
    }

    private func start() async {
        guard model.phase == .loading else { return }
        await model.load(card: card, app: app)
        if case .ready = model.phase {
            anchor(to: model.resumeParagraph)
            #if DEBUG
            if let paragraph = DevLaunch.scrollParagraph { anchor(to: paragraph) }
            if DevLaunch.searchQuery != nil {
                // 阅读器这一刻才刚出现，挂在它上面的 sheet 还没装好，当场设会被丢掉。
                // 真实路径（点菜单）不经过这一刻，所以只有这个开关要等一下。
                try? await Task.sleep(for: .milliseconds(600))
                searching = true
            }
            if let seq = DevLaunch.jumpToSeq,
               let hit = model.search(model.surface(at: seq) ?? "").first(where: { $0.id == seq }) {
                jump(to: hit)
            }
            if let seq = DevLaunch.tapSeq {
                model.tap(seq: seq, app: app)
                if let pick = DevLaunch.pickSense {
                    model.select(.init(inner: DevLaunch.openInner, senseId: pick))
                }
                if DevLaunch.markRequested { model.setMark(DevLaunch.markKind, app: app) }
            }
            #endif
        }
    }

    /// 打开时定位：设一次，并登记成「待对准」，排版量完之前每长高一次就再对准一次。
    private func anchor(to paragraph: Int?) {
        scrolledTo = paragraph ?? Self.headerID
        pendingAnchor = paragraph.map { Anchor(paragraph: $0, point: .top) }
    }

    /// 对准到哪一段的哪个位置。`nonce` 让「连着两次跳到同一处」也算一次新请求。
    struct Anchor: Equatable {
        let paragraph: Int
        let point: UnitPoint
        var nonce = UUID()
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
            anchor(to: model.resumeParagraph)
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
