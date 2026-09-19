import SwiftUI
import ERCore

/// 开发者选项（P8 §8）。
///
/// **直接控制后台，不内嵌管理台网页**——那些页面是给鼠标设计的，表格宽、按钮小，
/// 手机上点不动。后端一行没改：`require_admin` 本来就接受 `X-Admin-Secret`
/// 请求头（注释说的是「命令行，以及 AI 直接查诊断时用」）。
///
/// **只放六样**：跳天、任务、配置、服务端日志、状态，外加**重拉本地日志**。
/// 判据是「验收与开发期天天要用」——写在这里是因为「顺便再加一个」的门槛
/// 一旦没了，这一页就会长成第二个管理台。备份和恢复明确不做：在手机上下一个
/// 几十 MB 的数据库没有用，而「恢复」能把学习记录整份换掉，它该待在需要坐下来
/// 才能用的地方。
///
/// **第六样是 2026-09-18 加的**，用户当天定的。它在这里而不在设置里，
/// 理由同其余五样：日志从不回头改（那是设计），所以「服务端那份清干净了而设备
/// 手里还是脏的」只能靠整份重拉；而判断该不该重拉，需要看得懂日志的人。
struct DeveloperScreen: View {
    @Environment(AppModel.self) private var app

    @State private var secret = ""
    @State private var adminURL = ""
    @State private var clock: AdminClient.ClockStatus?
    @State private var tasks: AdminClient.TaskList?
    @State private var busy: String?
    @State private var failure: String?
    @State private var running: String?
    @State private var confirmingReset = false
    @State private var resetOutcome: AppModel.LocalLogReset?

    var body: some View {
        Form {
            credentialSection
            if app.admin != nil {
                clockSection
                taskSection
                linkSection
            }
            // **不在 `app.admin != nil` 里面。** 重拉本地日志走的是设备令牌
            // 那条普通的客户端路径，跟管理凭证无关——放进那个 `if` 会让它
            // 在没填管理密码时消失，而那时它照样该能用。
            resetSection
            if let failure {
                Section { Text(failure).foregroundStyle(.red).font(.footnote) }
            }
        }
        .navigationTitle("开发者选项")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            adminURL = app.connection.adminBaseURLOverride
            // 日志总数是按需算的（整份读盘），所以进这一页时算一次。
            app.refreshEventCount()
        }
        .task { await refresh() }
    }

    // MARK: 重拉本地日志

    private var resetSection: some View {
        Section {
            LabeledContent("本地事件", value: "\(app.eventCount) 条")
            LabeledContent("还没上报", value: "\(app.pendingEvents) 条")
            Button("丢掉本地日志，从服务器重新拉", role: .destructive) {
                confirmingReset = true
            }
            .disabled(app.engine == nil)
            if let resetOutcome {
                switch resetOutcome {
                case .done(let discarded, let adopted):
                    Text("丢掉 \(discarded) 条，重新拉回 \(adopted) 条。")
                        .font(.footnote).foregroundStyle(.secondary)
                case .refusedPending(let count):
                    // **拒绝要说清为什么**，否则它看起来像没反应。
                    Text("没做：还有 \(count) 条没上报，丢掉就真的没了。"
                         + "先联网让它们传上去，再来。")
                        .font(.footnote).foregroundStyle(.orange)
                case .failed(let why):
                    Text(why).font(.footnote).foregroundStyle(.red)
                }
            }
        } header: {
            Text("本地日志")
        } footer: {
            Text("学习记录的第一副本在这台设备上，服务器是会合点。"
                 + "日志只增不改，所以服务器那份修正过之后，"
                 + "这台设备手里那份要靠整份重拉才跟得上。"
                 + "**只在没有待上报的事件时才会执行。**")
        }
        .confirmationDialog("丢掉本地日志？", isPresented: $confirmingReset,
                            titleVisibility: .visible) {
            Button("丢掉并重新拉", role: .destructive) {
                Task {
                    resetOutcome = await app.resetLocalLog()
                    app.refreshEventCount()
                }
            }
            Button("算了", role: .cancel) {}
        } message: {
            Text("会先推一次。还有没上报的事件时不会执行。")
        }
    }

    // MARK: 凭证

    private var credentialSection: some View {
        Section {
            SecureField("管理密码", text: $secret)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            LabeledContent("当前密码", value: app.connection.adminSecretHint)
            TextField("管理地址（留空＝跟服务器同一台）", text: $adminURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            Button("保存") {
                app.connection.updateAdmin(secret: secret, baseURL: adminURL)
                secret = ""
                app.log?.write(.info, "admin.credential.saved", "管理凭证保存了")
                Task { await refresh() }
            }
            if !app.connection.adminSecret.isEmpty {
                Button("清除管理密码", role: .destructive) {
                    app.connection.clearAdminSecret()
                    clock = nil
                    tasks = nil
                    app.log?.write(.warn, "admin.credential.cleared", "管理密码被清除了")
                }
            }
        } header: {
            Text("管理凭证")
        } footer: {
            // 两把凭证在一个 App 里，界面上必须分得开。
            Text("这是**管理密码**，不是设备令牌——填错了会到处报 401。"
                 + "手机丢了它就跟着丢，那时只能改 ER_ADMIN_SECRET 再重启。")
        }
    }

    // MARK: 跳天

    /// 开发期天天用的那一个。**归零必须有**：在模拟的未来里通过验收什么也
    /// 证明不了，而忘了归零的下一次验收会拿着错的「今天」跑。
    private var clockSection: some View {
        Section {
            if let clock {
                LabeledContent("现在停在", value: clock.simulated
                               ? "第 +\(clock.offset_days) 天"
                               : "真实时间")
                LabeledContent("模拟的此刻", value: String(clock.simulated_now.prefix(16)))
                    .font(.footnote)
            }
            Button("跳到下一天") { Task { await advance(days: 1) } }
                .disabled(busy != nil)
            Button("归零", role: .destructive) { Task { await advance(days: nil) } }
                .disabled(busy != nil || clock?.simulated == false)
        } header: {
            Text("模拟时钟")
        } footer: {
            Text("复习的「今天」按它算，打卡日历也跟着它走。")
        }
    }

    // MARK: 任务

    private var taskSection: some View {
        Section {
            if let tasks {
                ForEach(tasks.tasks) { task in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(task.title).font(.body)
                            Spacer()
                            if running == task.name {
                                ProgressView()
                            } else {
                                Button("跑一次") { Task { await run(task.name) } }
                                    .buttonStyle(.bordered)
                                    .disabled(busy != nil)
                            }
                        }
                        HStack(spacing: 8) {
                            Text(task.enabled ? task.schedule : "已停用")
                            if let next = task.next_due_at {
                                Text("下次 \(String(next.prefix(16)))")
                            }
                            if let status = task.last_status {
                                Text("上次 \(status)")
                                    .foregroundStyle(status == "failed" ? .red : .secondary)
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        if let error = task.last_error, !error.isEmpty {
                            Text(error).font(.caption2).foregroundStyle(.red).lineLimit(3)
                        }
                    }
                    .padding(.vertical, 2)
                }
            } else {
                Text("还没读到任务列表。").foregroundStyle(.secondary)
            }
        } header: {
            Text("定时任务")
        } footer: {
            // 服务端那一侧就是在请求线程里跑的：这些活要几分钟，而一个立刻返回、
            // 然后安静失败的按钮，正是这个项目反复踩到的形状。
            Text("「跑一次」是**真的跑**，一篇文章要几分钟，屏幕会一直转。")
        }
    }

    private var linkSection: some View {
        Section {
            NavigationLink { AdminConfigScreen() } label: {
                Label("配置", systemImage: "slider.horizontal.3")
            }
            NavigationLink { AdminLogScreen() } label: {
                Label("服务端日志", systemImage: "server.rack")
            }
            NavigationLink { AdminStatusScreen() } label: {
                Label("服务状态", systemImage: "waveform.path.ecg")
            }
        } footer: {
            Text("备份与恢复不在这里——那是要坐下来才能做的事。")
        }
    }

    // MARK: 动作

    private func refresh() async {
        guard let admin = app.admin else { return }
        failure = nil
        do {
            async let status = admin.clock()
            async let list = admin.tasks()
            clock = try await status
            tasks = try await list
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    private func advance(days: Int?) async {
        guard let admin = app.admin else { return }
        busy = "clock"
        defer { busy = nil }
        do {
            clock = try await admin.advanceClock(days: days)
            app.log?.write(.info, "admin.clock.moved", "模拟时钟动了",
                           fields: ["offset": String(clock?.offset_days ?? 0)])
            // 跳天之后今日包和复习队列都不一样了，本地那份当场就过期了。
            await app.drain()
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    private func run(_ name: String) async {
        guard let admin = app.admin else { return }
        busy = name
        running = name
        defer { busy = nil; running = nil }
        do {
            _ = try await admin.runTask(name)
            app.log?.write(.info, "admin.task.ran", "手动跑了一次任务",
                           fields: ["task": name])
            await refresh()
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            app.log?.write(.warn, "admin.task.failed", "手动跑任务失败",
                           fields: ["task": name])
        }
    }
}
