import SwiftUI
import ERCore
import ERContract

/// 第四屏：设置（P8）。
///
/// **八个区，一屏放得下，不用滚**：账号、连接、阅读、今天的安排、存储、日志、
/// 开发者选项、关于。排序的依据是**用的频率倒过来**——连接是装完第一次填、
/// 之后再也不碰的东西，但它出问题时最要紧，所以靠上；朗读和字号天天可能动，
/// 反而不该占第一屏的位置（字号的入口在阅读屏的 `···` 里，调它要看着正文调）。
///
/// **能进二级页的都进了二级页**：连接、缓存、日志、开发者选项。
/// 一屏滚三次的设置页，找东西全靠记位置。
struct SettingsScreen: View {
    @Environment(AppModel.self) private var app
    @State private var model = SettingsModel()

    var body: some View {
        @Bindable var prefs = app.preferences

        NavigationStack {
            Form {
                accountSection
                connectionSection
                readingSection($prefs)
                serverSection
                storageSection
                logSection
                if app.preferences.developerUnlocked { developerSection }
                aboutSection
            }
            .navigationTitle("设置")
            .task { model.load(app) }
            .refreshable { model.load(app) }
            .alert("还没做", isPresented: $model.showLoginNotice) {
                Button("好", role: .cancel) {}
            } message: {
                Text("账号登录留给以后——将来几个人各自登录、各拿各的数据。"
                     + "现在用下面的设备令牌连接。")
            }
        }
    }

    // MARK: 账号

    /// **一行，点不进去，右边不带箭头。**
    ///
    /// 原本要做一张个人页，2026-09-15 缩成这一行：能放上去的只有名字、一行
    /// 「功能待开发」和一个说「还没做」的按钮，而连续打卡天数 P7 已经显示在
    /// 复习主界面上了——再放一遍是同一个数显示两处。
    /// **带箭头而点不动，比没有箭头更让人以为坏了。**
    private var accountSection: some View {
        Section {
            HStack(spacing: 14) {
                Image(systemName: "person.crop.circle.fill")
                    .font(.system(size: 40))
                    .foregroundStyle(.tertiary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.learnerName)
                        .font(.headline)
                    // `learner.level` 恒为 null，而 `capabilities.level_estimate`
                    // 为假。契约留这个标志位要区分的正是「没数据」和「没实现」，
                    // 而这里是它至今第一个读者。
                    Text(model.levelText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 4)
        } footer: {
            Text("这里显示的是这台设备连着的那份学习记录。要换成别的，在下面的「连接」里改。")
        }
    }

    // MARK: 连接

    private var connectionSection: some View {
        Section("连接") {
            // 将来的正路：账号 + 密码。这个 Phase 只画不接（决定 5）——
            // 点了说「还没做」，不是没反应。
            Button {
                model.showLoginNotice = true
            } label: {
                HStack {
                    Label("登录", systemImage: "person.badge.key")
                    Spacer()
                    Text("还没做").foregroundStyle(.tertiary)
                }
            }
            .tint(.primary)

            NavigationLink {
                ConnectionScreen()
            } label: {
                HStack {
                    Label("用设备令牌连接", systemImage: "link")
                    Spacer()
                    Text(app.connection.isConfigured ? "已连接" : "未配置")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: 阅读

    private func readingSection(_ prefs: Bindable<Preferences>) -> some View {
        Section {
            Picker("朗读口音", selection: prefs.accent) {
                ForEach(Preferences.Accent.allCases) { accent in
                    Text(accent.label).tag(accent)
                }
            }
            VStack(alignment: .leading) {
                HStack {
                    Text("朗读语速")
                    Spacer()
                    Text(String(format: "%.1f×", app.preferences.speechRate))
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                Slider(value: prefs.speechRate,
                       in: Preferences.speechRateRange, step: 0.05)
            }
            Toggle("点词自动朗读", isOn: prefs.speakOnTap)
        } header: {
            Text("阅读")
        } footer: {
            // 字号不放在这里是有理由的，而理由值得说出来——否则下一个人会
            // 觉得漏了一项，然后在这儿再加一个滑块。
            Text("正文字号在阅读界面的「···」里调，那里看得到效果。")
        }
    }

    // MARK: 今天的安排（只读）

    private var serverSection: some View {
        Section {
            LabeledContent("今天几篇", value: model.articleCountText)
            LabeledContent("「新备的」算几天", value: model.freshDaysText)
            LabeledContent("拼写强化", value: model.spellingText)
        } header: {
            Text("今天的安排")
        } footer: {
            Text("这几项由服务端决定，手机上只显示。要改，在管理控制台改。")
        }
    }

    // MARK: 存储

    private var storageSection: some View {
        Section("存储") {
            NavigationLink {
                CacheScreen()
            } label: {
                HStack {
                    Label("已缓存的文章", systemImage: "internaldrive")
                    Spacer()
                    Text(model.cacheSummary).foregroundStyle(.secondary)
                }
            }

            HStack {
                Label("待上报", systemImage: "tray.and.arrow.up")
                Spacer()
                Text("\(app.pendingEvents) 条").foregroundStyle(.secondary)
            }

            Button {
                Task {
                    model.draining = true
                    await app.drain()
                    model.load(app)
                    model.draining = false
                }
            } label: {
                if model.draining {
                    HStack { Text("正在上报"); Spacer(); ProgressView() }
                } else {
                    Text("立刻上报")
                }
            }
            .disabled(model.draining || app.engine == nil)

            if let sync = app.lastSync {
                // `SyncReport` 的四个数从 P5 起就返回了，一直没人显示。
                LabeledContent("上次同步", value: Self.describe(sync))
                    .font(.footnote)
            }
        }
    }

    static func describe(_ report: SyncReport) -> String {
        if report.offline { return "联不上，回头自己重试" }
        var parts = ["落 \(report.landed)"]
        if report.duplicates > 0 { parts.append("重复 \(report.duplicates)") }
        if report.rejected > 0 { parts.append("拒收 \(report.rejected)") }
        if report.remaining > 0 { parts.append("还剩 \(report.remaining)") }
        return parts.joined(separator: " · ")
    }

    // MARK: 日志

    private var logSection: some View {
        Section {
            NavigationLink {
                LogScreen()
            } label: {
                Label("日志", systemImage: "doc.text.magnifyingglass")
            }
            if let failure = app.storageFailure {
                Text(failure).foregroundStyle(.red).font(.footnote)
            }
        } footer: {
            Text("手机这边发生了什么。存三天，可以复制、分享、存成文件。")
        }
    }

    // MARK: 开发者选项

    private var developerSection: some View {
        Section {
            NavigationLink {
                DeveloperScreen()
            } label: {
                Label("开发者选项", systemImage: "wrench.and.screwdriver")
            }
        } footer: {
            Text("直接控制后台：跳天、任务、配置、服务端日志。用的是管理密码，不是设备令牌。")
        }
    }

    // MARK: 关于

    private var aboutSection: some View {
        Section("关于") {
            HStack {
                Text("版本")
                Spacer()
                Text(Self.versionText).foregroundStyle(.secondary).monospacedDigit()
            }
            // 连点七下开出开发者选项（决定 28）。理由不是保密——密码才是——
            // 而是这个 App 要装到别人手机上去，一打开设置就看见「开发者选项」，
            // 第一反应是这软件没做完。
            .contentShape(.rect)
            .onTapGesture { model.tapVersion(app) }

            if app.preferences.developerUnlocked {
                Button("藏起开发者选项", role: .destructive) {
                    app.preferences.developerUnlocked = false
                }
            }
        }
    }

    /// 构建号由 `testflight.yml` 用 `github.run_number` 写进去，所以它对得回
    /// 具体哪一次 CI、哪一个提交。没有它，别人反馈「不好使」对不上代码。
    static var versionText: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(short) (\(build))"
    }
}

/// 设置屏要显示的那些数，集中在一处算。
///
/// **今日包是这些数唯一的来源**，而它就在本地——所以这一屏离线也是对的，
/// 不需要为了显示「每天几篇」去打一次网络。
@MainActor
@Observable
final class SettingsModel {
    var learnerName = "本人"
    var levelText = "词汇量估计 · 功能待开发"
    var articleCountText = "—"
    var freshDaysText = "—"
    var spellingText = "—"
    var cacheSummary = "—"
    var draining = false
    var showLoginNotice = false

    private var versionTaps = 0

    func load(_ app: AppModel) {
        if let data = app.dayCache?.load(), let package = try? DayPackage(raw: data) {
            learnerName = package.learner.name
            // 没实现和没数据是两回事，而屏幕上「空白」和「0」都说不清是哪一种。
            if package.capabilities.level_estimate {
                levelText = package.learner.level.map { "词汇量估计 \($0)" }
                    ?? "词汇量估计 · 还没测"
            } else {
                levelText = "词汇量估计 · 功能待开发"
            }
            articleCountText = "\(package.articleCount) 篇"
            freshDaysText = "\(package.settings.fresh_days) 天"
            spellingText = package.settings.spelling_enabled ? "开" : "关"
        }

        if let cache = app.cache {
            let sizes = cache.bodySizes()
            let bytes = sizes.values.reduce(0, +)
            cacheSummary = sizes.isEmpty
                ? "没有缓存"
                : "\(sizes.count) 篇 · \(Self.readable(bytes))"
        }
    }

    /// 七下。少一下都不算——这个数字本身不重要，重要的是它不会被误触。
    func tapVersion(_ app: AppModel) {
        guard !app.preferences.developerUnlocked else { return }
        versionTaps += 1
        if versionTaps >= 7 {
            versionTaps = 0
            app.preferences.developerUnlocked = true
            app.log?.write(.info, "developer.unlocked", "开发者选项打开了")
        }
    }

    static func readable(_ bytes: Int) -> String {
        let units = ["B", "KB", "MB", "GB"]
        var value = Double(bytes)
        var index = 0
        while value >= 1024, index < units.count - 1 {
            value /= 1024
            index += 1
        }
        return String(format: index == 0 ? "%.0f %@" : "%.1f %@", value, units[index])
    }
}
