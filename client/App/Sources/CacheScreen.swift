import SwiftUI
import ERCore

/// 缓存管理（P8 §5）。
///
/// **Core 里三个方法早就写好了、测过了，一个出口都没有。**`Cache.swift` 的模块
/// 注释写着「缓存永不自动清理，读过没读过都留着，直到学习者说否」——而「说否」
/// 这件事此前没有地方说。
///
/// **清掉的是正文，元信息留着**：清过的文章仍然在列表里、仍然显示读到哪，
/// 点开重新拉。这是 Core 已经定好的语义，界面照搬，**不要自己发明「删除文章」**。
struct CacheScreen: View {
    @Environment(AppModel.self) private var app

    @State private var rows: [Row] = []
    @State private var confirmingClearAll = false

    struct Row: Identifiable {
        let meta: ArticleMeta
        let bytes: Int
        var id: Int { meta.id }
    }

    var body: some View {
        List {
            Section {
                LabeledContent("已缓存", value: "\(rows.count) 篇")
                LabeledContent("占用", value: SettingsModel.readable(totalBytes))
            } footer: {
                Text("正文约 300 KB 一篇。清掉之后标题和读到哪都还在，点开会重新拉。")
            }

            if rows.isEmpty {
                Section {
                    Text("现在没有缓存的正文。").foregroundStyle(.secondary)
                }
            } else {
                Section("按篇") {
                    ForEach(rows) { row in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(row.meta.title).font(.body).lineLimit(2)
                            HStack(spacing: 8) {
                                Text(SettingsModel.readable(row.bytes)).monospacedDigit()
                                if row.meta.readAt != nil {
                                    Text("已读完")
                                } else if row.meta.percent > 0 {
                                    Text("读到 \(Int(row.meta.percent * 100))%")
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                        .swipeActions {
                            Button("清掉正文", role: .destructive) { clear([row.meta.id]) }
                        }
                    }
                }

                Section {
                    Button("全部清掉", role: .destructive) { confirmingClearAll = true }
                }
            }
        }
        .navigationTitle("已缓存的文章")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear(perform: reload)
        .confirmationDialog("清掉全部正文？", isPresented: $confirmingClearAll,
                            titleVisibility: .visible) {
            Button("清掉 \(rows.count) 篇", role: .destructive) {
                clear(rows.map(\.meta.id))
            }
            Button("算了", role: .cancel) {}
        } message: {
            Text("标题、读到哪、还没上报的标记都留着。离线时清掉的文章打不开，"
                 + "要等联网再拉。")
        }
    }

    private var totalBytes: Int { rows.reduce(0) { $0 + $1.bytes } }

    private func reload() {
        guard let cache = app.cache else { rows = []; return }
        let sizes = cache.bodySizes()
        rows = cache.articles()
            .filter { sizes[$0.id] != nil }
            .map { Row(meta: $0, bytes: sizes[$0.id] ?? 0) }
            .sorted { $0.bytes > $1.bytes }
    }

    /// **清理只碰正文目录**（P5 决定 8）。发件箱在另一个目录里，而它装着这台
    /// 设备上唯一没有副本的东西——连同缓存一起删掉的话，屏幕上什么都不会说，
    /// 那个词也永远进不了复习队列。
    private func clear(_ ids: [Int]) {
        guard let cache = app.cache else { return }
        let removed = (try? cache.clearBodies(ids)) ?? 0
        app.log?.write(.info, "cache.cleared", "清掉了正文",
                       fields: ["count": String(removed)])
        reload()
    }
}
