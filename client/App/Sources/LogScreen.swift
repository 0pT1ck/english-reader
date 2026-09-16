import SwiftUI
import ERCore

/// 客户端日志（P8 §9）。
///
/// **两套日志，这是手机这一套。**服务端那套在 `logs.db`、30 天、走开发者选项查；
/// 这一套记的是手机这边发生了什么：请求发出去没有、标记落盘没有、缓存清了几篇。
/// 两套之间靠 `trace_id` 接起来——**复制一个 trace 过去，就能把服务器那半边
/// 的完整链路捞出来**。
///
/// **复制、分享、导出到本地：一个系统分享面板全办了。**`ShareLink` 出来那一张
/// 面板里，拷贝、发给自己、存进「文件」App 都在，不用自己做三个按钮。
/// 导出的是纯文本——截图搜不了，也贴不进对话里。
struct LogScreen: View {
    @Environment(AppModel.self) private var app

    @State private var entries: [LogEntry] = []
    @State private var floor: LogLevel = .info
    @State private var confirmingClear = false

    var body: some View {
        @Bindable var prefs = app.preferences

        List {
            Section {
                Picker("只看", selection: $floor) {
                    Text("全部").tag(LogLevel.debug)
                    Text("INFO").tag(LogLevel.info)
                    Text("WARN").tag(LogLevel.warn)
                    Text("ERROR").tag(LogLevel.error)
                }
                .pickerStyle(.segmented)

                Toggle("记录 DEBUG", isOn: $prefs.verboseLog)
                    .onChange(of: app.preferences.verboseLog) { _, _ in
                        app.applyLogLevel()
                    }
            } footer: {
                Text("DEBUG 平时不记——查一件事的时候才打开。日志存三天，过期自动清掉。")
            }

            if let failure = app.log?.lastFailure {
                Section {
                    // 日志自己写不进去的时候，它没法用日志说这件事。
                    Text(failure).foregroundStyle(.red)
                }
            }

            if entries.isEmpty {
                Section { Text("这一档没有记录。").foregroundStyle(.secondary) }
            } else {
                Section("\(entries.count) 条 · 新的在上面") {
                    ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
                        LogRowView(entry: entry)
                    }
                }
            }
        }
        .navigationTitle("日志")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if let text = app.log?.exportText(), !text.isEmpty {
                    // 一张面板办三件事：拷贝、发给自己、存进「文件」。
                    ShareLink(item: text) {
                        Image(systemName: "square.and.arrow.up")
                    }
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button("清空", role: .destructive) { confirmingClear = true }
                    .disabled(entries.isEmpty)
            }
        }
        .onAppear(perform: reload)
        .onChange(of: floor) { _, _ in reload() }
        .confirmationDialog("清空日志？", isPresented: $confirmingClear,
                            titleVisibility: .visible) {
            Button("清空", role: .destructive) {
                try? app.log?.clear()
                reload()
            }
            Button("算了", role: .cancel) {}
        } message: {
            Text("只清手机上这一份，服务端的日志不受影响。")
        }
    }

    private func reload() {
        entries = app.log?.entries(minimum: floor, limit: 300) ?? []
    }
}

private struct LogRowView: View {
    let entry: LogEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text(entry.level.label)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(color)
                Text(entry.event)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                Spacer()
                Text(entry.at, style: .time)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
            }
            Text(entry.message).font(.callout)
            if let traceId = entry.traceId {
                // 长按复制：这个 id 是拿去服务端日志里查的钥匙，
                // 手打它是不可能的。
                Text("trace \(traceId)")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if !entry.fields.isEmpty {
                Text(entry.fields.keys.sorted()
                        .map { "\($0)=\(entry.fields[$0] ?? "")" }
                        .joined(separator: "  "))
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
    }

    private var color: Color {
        switch entry.level {
        case .debug: return .secondary
        case .info: return .blue
        case .warn: return .orange
        case .error: return .red
        }
    }
}
