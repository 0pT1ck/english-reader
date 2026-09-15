import SwiftUI
import ERCore

/// 服务端日志（P8 §8 决定 26 的第四样）。
///
/// **`trace_id` 是这一页存在的理由。**手机上的日志记下一个 trace，
/// 把它填进上面那个框，服务器那半边的完整链路就出来了——
/// 包括失败时落盘的那些 DEBUG。这件事本来要坐到电脑前才做得成。
struct AdminLogScreen: View {
    @Environment(AppModel.self) private var app

    @State private var records: [AdminClient.LogRow] = []
    @State private var level = "WARNING"
    @State private var trace = ""
    @State private var loading = false
    @State private var failure: String?

    private let levels = ["DEBUG", "INFO", "WARNING", "ERROR"]

    var body: some View {
        List {
            Section {
                Picker("级别", selection: $level) {
                    ForEach(levels, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.segmented)
                TextField("trace_id（从手机日志里复制过来）", text: $trace)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.caption.monospaced())
                Button("查") { Task { await load() } }
                    .disabled(loading)
            } footer: {
                Text("填了 trace 就只看那一条链路，级别这时不起作用。")
            }

            if let failure {
                Section { Text(failure).foregroundStyle(.red).font(.footnote) }
            }

            if loading {
                Section { HStack { Text("正在查"); Spacer(); ProgressView() } }
            } else if records.isEmpty {
                Section { Text("没有记录。").foregroundStyle(.secondary) }
            } else {
                Section("\(records.count) 条 · 新的在上面") {
                    ForEach(records) { record in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 8) {
                                Text(record.level)
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(color(record.level))
                                Text(record.event)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                Spacer()
                                // `2026-09-15T18:42:03+08:00` → `18:42:03`。
                                // `dropFirst` 对短字符串是安全的，`index(offsetBy:)` 不是。
                                Text(String(record.ts.dropFirst(11).prefix(8)))
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                    .monospacedDigit()
                            }
                            Text(record.message).font(.callout)
                            if let traceId = record.trace_id {
                                Text("trace \(traceId)")
                                    .font(.caption2.monospaced())
                                    .foregroundStyle(.tertiary)
                                    .textSelection(.enabled)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
        }
        .navigationTitle("服务端日志")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        guard let admin = app.admin else { return }
        loading = true
        defer { loading = false }
        do {
            let trimmed = trace.trimmingCharacters(in: .whitespacesAndNewlines)
            let list = try await admin.logs(level: trimmed.isEmpty ? level : nil,
                                            traceId: trimmed.isEmpty ? nil : trimmed,
                                            limit: 120)
            records = list.records
            failure = nil
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    private func color(_ level: String) -> Color {
        switch level {
        case "ERROR", "CRITICAL": return .red
        case "WARNING": return .orange
        case "INFO": return .blue
        default: return .secondary
        }
    }
}
