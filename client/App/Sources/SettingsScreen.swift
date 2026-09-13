import SwiftUI

/// 第四屏：设置。**只有连接这一件事**（决定 31）。
///
/// 缓存管理、占用统计、「全部下载」都不在这个 Phase 里。之所以设置屏还是得做，
/// 是因为设备令牌由管理控制台签发、**只显示一次**，客户端总得有地方接住它。
struct SettingsScreen: View {
    @Environment(AppModel.self) private var app

    @State private var baseURL = ""
    @State private var token = ""
    @State private var saved = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://192.168.x.x:8000", text: $baseURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                } header: {
                    Text("服务器地址")
                } footer: {
                    if !baseURL.isEmpty && app.connection.url == nil {
                        Text("这个地址拼不出来，检查一下有没有带 http://")
                            .foregroundStyle(.red)
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
                    if app.connection.isConfigured {
                        LabeledContent("状态", value: "已连接配置")
                    }
                } footer: {
                    if app.pendingEvents > 0 {
                        // 「我标的东西传上去了没有」在这个 Phase 里没有别的出口，
                        // 所以这一行留着——它不是缓存管理，是发件箱的状态。
                        Text("还有 \(app.pendingEvents) 条标记等着上报，联网后自动补发。")
                    }
                }

                if let failure = app.storageFailure {
                    Section("本地存储") {
                        Text(failure).foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("设置")
            .onAppear { baseURL = app.connection.baseURL }
            .alert("已保存", isPresented: $saved) {
                Button("好", role: .cancel) {}
            }
        }
    }

    private func save() {
        app.connection.update(baseURL: baseURL, token: token)
        token = ""
        app.rebuildEngine()
        saved = true
        Task { await app.drain() }
    }
}
