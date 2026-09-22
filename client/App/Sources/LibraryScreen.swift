import SwiftUI
import ERCore
import ERContract

/// 第一屏：阅读列表。
///
/// 「文章 / 真题」这条分段正好对上 Core 已有的两个列表（P5 决定 19），内核一行
/// 没改。真题下面再排一条四级 / 六级 / 考研，样式相同（决定 8）。
struct LibraryScreen: View {
    @Environment(AppModel.self) private var app
    @State private var model = LibraryModel()

    var body: some View {
        NavigationStack {
            content
                .libraryBackground()
                .navigationTitle("阅读")
                .navigationDestination(for: ArticleCard.self) { card in
                    ReaderScreen(card: card)
                }
        }
        .task { await model.load(app) }
        .onChange(of: model.shelf) { _, _ in Task { await model.load(app) } }
        .onChange(of: model.paper) { _, _ in Task { await model.load(app) } }
    }

    @ViewBuilder
    private var content: some View {
        if !app.connection.isConfigured {
            NotConfiguredNotice()
        } else {
            List {
                Section {
                    Picker("书架", selection: $model.shelf) {
                        ForEach(LibraryModel.Shelf.allCases, id: \.self) {
                            Text($0.title).tag($0)
                        }
                    }
                    .pickerStyle(.segmented)

                    if model.shelf == .exam {
                        Picker("卷别", selection: $model.paper) {
                            ForEach(LibraryModel.Paper.allCases, id: \.self) {
                                Text($0.title).tag($0)
                            }
                        }
                        .pickerStyle(.segmented)
                    }
                }
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)

                if let notice = model.notice {
                    Section { OfflineRow(text: notice) }
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                }

                // 第一次进这个书架、盘上什么都没有时才转圈。
                if model.loading && model.cards.isEmpty {
                    Section {
                        HStack { Spacer(); ProgressView(); Spacer() }
                            .padding(.vertical, 24)
                    }
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
                }

                ForEach(model.cards) { card in
                    NavigationLink(value: card) {
                        ArticleCardRow(card: card)
                    }
                    .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .refreshable { await model.load(app, force: true) }
        }
    }
}

/// 卡片本身。
///
/// **等高**（决定 10）：标题最多两行、概括一行，四行的骨架固定在那儿，
/// 哪一格没内容就空着。高度随内容变的话，一屏里三张卡会长短不齐。
private struct ArticleCardRow: View {
    let card: ArticleCard

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(card.topline)
                Spacer()
                Text(card.preparedLine)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)

            Text(card.title)
                .font(.headline)
                .lineLimit(2, reservesSpace: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            Text(card.summary ?? " ")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            HStack {
                Text(card.countsLine)
                Spacer()
                Text(card.stateLabel)
                    .foregroundStyle(card.isRead ? .secondary : .primary)
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

/// 离线时说的话（决定 33）。
///
/// **转圈继续转，旁边写清楚原因**——意思是还在重试，网一回来自己就好。
/// 只转圈是最糟的答案：你分不出它在努力还是已经放弃了。
private struct OfflineRow: View {
    let text: String

    var body: some View {
        HStack(spacing: 10) {
            ProgressView()
            Text(text)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }
}

private struct NotConfiguredNotice: View {
    var body: some View {
        ContentUnavailableView {
            Label("还没连上后端", systemImage: "link.badge.plus")
        } description: {
            Text("到「设置」里填服务器地址和设备令牌。令牌在管理控制台签发，只显示一次。")
        }
    }
}

@MainActor
@Observable
final class LibraryModel {
    enum Shelf: CaseIterable, Hashable {
        case generated, exam
        var title: String { self == .generated ? "文章" : "真题" }
    }

    enum Paper: String, CaseIterable, Hashable {
        case cet4, cet6, kaoyan
        var title: String {
            switch self {
            case .cet4: "四级"
            case .cet6: "六级"
            case .kaoyan: "考研"
            }
        }
    }

    var shelf: Shelf = .generated
    var paper: Paper = .cet6
    private(set) var cards: [ArticleCard] = []
    private(set) var notice: String?

    private var source: String { shelf == .generated ? "generated" : paper.rawValue }
    private var cacheKey: String { source }

    /// 拉列表。
    ///
    /// **服务端已经只返回准备好的文章**（`library_articles` 的第一个条件就是
    /// `status = 'ready'`），所以决定 32「没准备好的不出现在列表里」在这里
    /// 是白送的，客户端一行过滤都不用写。
    ///
    /// shelf 传 `all`：决定 6 说暂不分「今天 / 往期」，一个列表按序号排。
    ///
    /// **单飞:同一时刻只有一趟**（2026-09-19，Mac 上复现）。`.task` 在切走
    /// 这一格时被取消、切回来时重新跑——和 `ReviewModel.load()`、
    /// `AppModel.drain()` 是同一个形状（那两处的注释已经写过这个坑），
    /// 而这里当时漏了同一处守卫。直接在这个 App 进程里对同一个 `LibraryModel`
    /// 触发两次并发 `load()` 实测：两次都各自打了一次 `/v1/client/library`，
    /// 没有任何东西挡它们。快速切换标签页时这会在阅读这一格上反复发生，
    /// 而它和复习那几个请求走的是同一个 `SyncEngine` actor、同一条隧道。
    private var isLoading = false

    func load(_ app: AppModel, force: Bool = false) async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        guard let engine = app.engine else {
            notice = app.connection.isConfigured ? "连接还没建好" : nil
            return
        }
        let key = cacheKey

        // **先摆缓存，再问服务端**（2026-09-15 真机之后改）。
        //
        // 原先是在线优先、只有断网才回退缓存——于是点一下「真题」，屏幕上
        // 还是上一个书架的内容，一秒多之后突然换掉。**看着像没反应**，
        // 而那一秒里答案其实就在盘上。
        //
        // 换书架时那份缓存是**这个书架自己的**（`cacheKey` 按 source 分），
        // 所以不会出现「先闪一下别人的列表」。没有缓存就清空并说一声，
        // 也比停在上一个书架上诚实。
        if let cached = app.library?.load(key: key) {
            apply(cached)
            notice = nil
        } else {
            cards = []
            loading = true
        }
        defer { loading = false }

        do {
            let response = try await engine.library(shelf: "all", source: source)
            apply(response)
            notice = nil
            if let data = try? JSONEncoder().encode(response) {
                app.library?.store(data, key: key)
            }
            // 拿到列表顺便把元信息喂给 Core 的缓存：清理缓存之后标题、字数、
            // 进度还在，靠的就是它（P5 决定 10）。
            app.cache?.rememberQuietly(response.articles)
        } catch let error as TransportError {
            if case .offline = error {
                if let cached = app.library?.load(key: key) { apply(cached) }
                notice = "离线，连不上服务器"
            } else {
                notice = describe(error)
            }
        } catch {
            notice = "拉列表失败：\(error.localizedDescription)"
        }
    }

    /// 正在等第一份数据。**只有在没有缓存可摆的时候才为真**——
    /// 有缓存时屏幕上已经有东西了，再转一个圈只是噪音。
    private(set) var loading = false

    private func apply(_ response: Components.Schemas.LibraryResponse) {
        let built = response.articles.map(ArticleCard.init(library:))
        // 按序号排（决定 8，排序功能不做）。生成文新的在前——每天备的稿
        // 堆在后面等于永远看不见；真题按卷子本来的顺序。
        cards = shelf == .generated
            ? built.sorted { $0.id > $1.id }
            : built.sorted { $0.id < $1.id }
    }

    private func describe(_ error: TransportError) -> String {
        switch error {
        case .offline(let detail): "离线，连不上服务器（\(detail)）"
        case .server(let status, _) where status == 401:
            "令牌不对或已吊销，到设置里换一个"
        case .server(let status, _): "服务器出错了（HTTP \(status)）"
        case .malformed(let detail): "服务器回的东西看不懂：\(detail)"
        case .upgradeRequired: "这份 App 太旧了，服务端不收——请更新到最新版本"
        }
    }
}

extension ArticleCache {
    /// 把列表里的元信息记下来，出错就算了——它是便利，不是正确性的一部分。
    func rememberQuietly(_ articles: [Components.Schemas.LibraryArticle]) {
        let metas = articles.map {
            ArticleMeta(id: $0.id, title: $0.title, source: $0.source,
                        wordCount: $0.word_count, sentenceCount: $0.sentence_count,
                        readAt: $0.read_at, percent: $0.percent)
        }
        try? remember(metas)
    }
}
