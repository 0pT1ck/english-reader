import SwiftUI
import ERCore

/// 服务状态（P8 §8 决定 26 的第五样）。
///
/// **一眼看出「有没有事」**，不是把管理台的状态页搬过来：四个库多大、
/// 过去一天各级别日志多少、装了哪些模块。事件订阅表和设备清单在手机上
/// 帮不上忙，它们只会把这一屏挤成一张读不动的表格。
struct AdminStatusScreen: View {
    @Environment(AppModel.self) private var app

    @State private var status: AdminClient.ServerStatus?
    @State private var failure: String?

    var body: some View {
        List {
            if let failure {
                Section { Text(failure).foregroundStyle(.red).font(.footnote) }
            }
            if let status {
                Section("服务") {
                    LabeledContent("版本", value: status.version)
                    LabeledContent("服务端时间", value: String(status.time.prefix(19)))
                        .font(.footnote)
                    if status.dev_mode {
                        Label("开发模式开着", systemImage: "hammer")
                            .foregroundStyle(.orange)
                    }
                }

                Section {
                    ForEach(status.databases.keys.sorted(), id: \.self) { name in
                        if let database = status.databases[name] {
                            LabeledContent(name) {
                                Text(database.exists
                                     ? SettingsModel.readable(database.size_bytes)
                                     : "不存在")
                                    .foregroundStyle(database.exists ? .secondary : .red)
                                    .monospacedDigit()
                            }
                        }
                    }
                } header: {
                    Text("数据库")
                } footer: {
                    // 「丢了会怎样」分两类，这一行是为了别在手机上误判。
                    Text("learning 与 content 丢不得；dictionary 可以从 ECDICT 重建，"
                         + "logs 可以丢。")
                }

                Section("过去一天的日志") {
                    if status.logs_last_24h.isEmpty {
                        Text("一条都没有").foregroundStyle(.secondary)
                    } else {
                        ForEach(status.logs_last_24h.keys.sorted(), id: \.self) { level in
                            LabeledContent(level,
                                           value: "\(status.logs_last_24h[level] ?? 0)")
                                .foregroundStyle(level == "ERROR" || level == "CRITICAL"
                                                 ? .red : .primary)
                                .monospacedDigit()
                        }
                    }
                }

                Section("模块 · \(status.modules.count) 个") {
                    ForEach(status.modules) { module in
                        LabeledContent(module.title, value: module.name)
                            .font(.footnote)
                    }
                }
            } else if failure == nil {
                Section { HStack { Text("正在读"); Spacer(); ProgressView() } }
            }
        }
        .navigationTitle("服务状态")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        guard let admin = app.admin else { return }
        do {
            status = try await admin.status()
            failure = nil
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }
}
