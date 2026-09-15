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
                    // 带 content 闭包的 `LabeledContent` 第一个参数要
                    // `LocalizedStringKey`，而这里的名字是个变量——
                    // 所以两边都显式给 `Text`，别让重载去猜。
                    ForEach(databaseNames(status), id: \.self) { name in
                        if let database = status.databases[name] {
                            LabeledContent {
                                // 三元两边要同一个类型：`.secondary` 是
                                // `HierarchicalShapeStyle`，`.red` 是 `Color`，
                                // 混着写的话整个闭包推断失败，而报错会指向
                                // `ForEach` 的另一个重载，看上去跟这里无关。
                                Text(database.exists
                                     ? SettingsModel.readable(database.size_bytes)
                                     : "不存在")
                                    .foregroundStyle(database.exists
                                                     ? Color.secondary : Color.red)
                                    .monospacedDigit()
                            } label: {
                                Text(name)
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
                        ForEach(logLevels(status), id: \.self) { level in
                            LabeledContent {
                                Text("\(status.logs_last_24h[level] ?? 0)")
                                    .monospacedDigit()
                            } label: {
                                Text(level)
                                    .foregroundStyle(level == "ERROR" || level == "CRITICAL"
                                                     ? Color.red : Color.primary)
                            }
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

    /// 先算成数组再交给 `ForEach`：字典的 `keys.sorted()` 直接写在参数位置上，
    /// 会让泛型推断连着后面几行一起失败，而报错指的是别的地方。
    private func databaseNames(_ status: AdminClient.ServerStatus) -> [String] {
        status.databases.keys.sorted()
    }

    private func logLevels(_ status: AdminClient.ServerStatus) -> [String] {
        status.logs_last_24h.keys.sorted()
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
