import SwiftUI

/// 文内搜索（P12 决定 ㉑，2026-09-23 用户加的）：在这一篇里找一个词，点一行跳过去。
///
/// 入口在阅读屏右上角的「选项」里。**每一处列一行**，显示它所在的整句、
/// 把它加粗——同一个词在一篇里常出现好几次，只给「第几处」的话分不出该去哪一处。
/// 匹配规则在 `ReaderModel.search`。
struct ArticleSearchSheet: View {
    let model: ReaderModel
    let onPick: (ReaderModel.SearchHit) -> Void

    @State private var query: String
    @FocusState private var focused: Bool
    @Environment(\.dismiss) private var dismiss

    init(model: ReaderModel, initialQuery: String = "",
         onPick: @escaping (ReaderModel.SearchHit) -> Void) {
        self.model = model
        self.onPick = onPick
        _query = State(initialValue: initialQuery)
    }

    var body: some View {
        NavigationStack {
            List {
                if !hits.isEmpty {
                    Section("\(hits.count) 处") {
                        ForEach(hits) { hit in
                            Button { onPick(hit) } label: {
                                // 跟复习屏同一个组件：按字符区间加粗，不按字符串搜——
                                // 同一个词在一句里出现两次时，搜会命中错的那一个。
                                HighlightedText(text: hit.sentence, highlight: hit.highlight)
                                    .font(.callout)
                                    .foregroundStyle(.primary)
                                    .contentShape(Rectangle())
                            }
                            // 不用列表按钮的默认着色：整行变蓝的话，加粗的那个词就不显眼了。
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            .overlay {
                if !query.trimmingCharacters(in: .whitespaces).isEmpty && hits.isEmpty {
                    ContentUnavailableView.search(text: query)
                }
            }
            .navigationTitle("在这篇里找")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "单词或词组")
            .searchFocused($focused)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                }
            }
            .onAppear { focused = true }
        }
        .presentationDetents([.medium, .large])
    }

    private var hits: [ReaderModel.SearchHit] { model.search(query) }
}
