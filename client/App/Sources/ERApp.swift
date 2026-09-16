import SwiftUI

/// 三格标签栏：阅读 / 复习 / 设置（P6 决定 29），默认落在第一格（决定 9）。
///
/// P6 时复习那格点进去说「正在开发中」，**P7 把它做出来了**，
/// P8 又给设置那格装上了八个区。这行注释 2026-09-15 订正——
/// 上一版还写着「正在开发中」，而那已经不成立了半个 Phase
/// （坑 §7.1：注释描述的是意图，不保证实现跟上了，反过来也一样）。
@main
struct EnglishReaderApp: App {
    @State private var app = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            TabView {
                Tab("阅读", systemImage: "book") {
                    LibraryScreen()
                }
                Tab("复习", systemImage: "arrow.triangle.2.circlepath") {
                    ReviewScreen()
                }
                Tab("设置", systemImage: "gearshape") {
                    SettingsScreen()
                }
            }
            .environment(app)
            .task { await app.drain() }
            .onChange(of: scenePhase) { _, phase in
                // 回到前台就把攒着的事件发一次。**离线时写下的标记要自己回去**，
                // 不该等用户想起来去点什么。
                if phase == .active { Task { await app.drain() } }
            }
        }
    }
}
