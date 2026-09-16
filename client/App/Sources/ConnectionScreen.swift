import SwiftUI
import ERCore

/// 连接的二级页：地址、令牌、测试、清除。
///
/// **这一页将来会被登录取代**（§3 决定 5、6），所以它补的是「在那之前必须能用」
/// 的那三件——测试连接、清除令牌，以及把令牌说清楚。
struct ConnectionScreen: View {
    @Environment(AppModel.self) private var app

    @State private var baseURL = ""
    @State private var token = ""
    @State private var probe: Probe = .idle
    @State private var confirmingClear = false

    /// **三种结果必须分得开。**这一页此前唯一的校验是「URL 拼不拼得出来」，
    /// 而「令牌不对」和「网络断了」在屏幕上长得一模一样——那正是坑 §6.7
    /// 说的形状：不是「有没有报错」，是「它区分得开吗」。
    enum Probe: Equatable {
        case idle
        case running
        case reachable(String)
        case unauthorized
        case offline(String)
        case broken(String)
    }

    var body: some View {
        Form {
            Section {
                TextField("https://reader.example.com", text: $baseURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            } header: {
                Text("服务器地址")
            } footer: {
                if !baseURL.isEmpty && parsedURL == nil {
                    Text("这个地址拼不出来，检查一下有没有带 https://").foregroundStyle(.red)
                } else {
                    Text("后端跑在哪台机器上就填哪个地址。")
                }
            }

            Section {
                // 令牌**永远不回显**，框里平时是空的：留空保存＝不改它。
                SecureField("粘贴管理控制台给的令牌", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                LabeledContent("当前令牌", value: app.connection.tokenHint)
            } header: {
                Text("设备令牌")
            } footer: {
                Text("在管理控制台签发，只显示一次。这里不会把它再显示出来——它是凭证。")
            }

            Section {
                Button("保存") { save() }
                    .disabled(baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("测试连接") { Task { await test() } }
                    .disabled(probe == .running || app.engine == nil)
                probeRow
            } footer: {
                Text("测试会真的打一次服务器，分得出「连不上」和「令牌不对」。")
            }

            Section {
                Button("清除令牌", role: .destructive) { confirmingClear = true }
                    .disabled(app.connection.token.isEmpty)
            } footer: {
                // 清掉之后要重新去管理台签发，那不是个便宜的动作。
                Text("清掉之后要回管理控制台重新签发一个。"
                     + "**不会**动缓存和还没上报的标记——那些是你自己的东西。")
            }
        }
        .navigationTitle("连接")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { baseURL = app.connection.baseURL }
        .confirmationDialog("清除令牌？", isPresented: $confirmingClear, titleVisibility: .visible) {
            Button("清除", role: .destructive) {
                app.connection.clearToken()
                app.rebuildEngine()
                app.log?.write(.warn, "connection.token.cleared", "设备令牌被清除了")
                probe = .idle
            }
            Button("算了", role: .cancel) {}
        } message: {
            Text("缓存和待上报的标记都留着。")
        }
    }

    private var parsedURL: URL? {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed),
              url.scheme != nil, url.host != nil else { return nil }
        return url
    }

    @ViewBuilder
    private var probeRow: some View {
        switch probe {
        case .idle:
            EmptyView()
        case .running:
            HStack { Text("正在试"); Spacer(); ProgressView() }
        case .reachable(let name):
            Label("通了 · 这份学习记录是「\(name)」的", systemImage: "checkmark.circle")
                .foregroundStyle(.green)
        case .unauthorized:
            Label("令牌不对（401）——地址是通的，凭证被拒了", systemImage: "key.slash")
                .foregroundStyle(.red)
        case .offline(let reason):
            Label("连不上：\(reason)", systemImage: "wifi.slash")
                .foregroundStyle(.orange)
        case .broken(let detail):
            Label(detail, systemImage: "questionmark.circle").foregroundStyle(.orange)
        }
    }

    private func save() {
        app.connection.update(baseURL: baseURL, token: token)
        token = ""
        app.rebuildEngine()
        probe = .idle
        app.log?.write(.info, "connection.updated", "连接设置保存了",
                       fields: ["host": app.connection.url?.host ?? "?"])
        Task { await app.drain() }
    }

    /// 用日历那个端点探，**它是最轻的一个**：不下发全文，服务端也不用组装今日包。
    private func test() async {
        guard let engine = app.engine else { return }
        probe = .running
        do {
            let calendar = try await engine.calendar(days: 1)
            probe = .reachable(calendar.learner.name)
            app.log?.write(.info, "connection.probe.ok", "测试连接通了")
        } catch let error as TransportError {
            switch error {
            case .server(let status, _) where status == 401 || status == 403:
                probe = .unauthorized
                app.log?.write(.warn, "connection.probe.unauthorized", "令牌被拒")
            case .server(let status, let body):
                probe = .broken("服务端回了 \(status)：\(body.prefix(120))")
                app.log?.write(.warn, "connection.probe.failed", "服务端拒绝了",
                               fields: ["status": String(status)])
            case .offline(let reason):
                probe = .offline(reason)
                app.log?.write(.warn, "connection.probe.offline", "联不上")
            case .malformed(let detail):
                probe = .broken("回来的内容看不懂——可能不是这个服务：\(detail.prefix(80))")
                app.log?.write(.warn, "connection.probe.malformed", "响应看不懂")
            }
        } catch {
            probe = .broken(error.localizedDescription)
        }
    }
}
