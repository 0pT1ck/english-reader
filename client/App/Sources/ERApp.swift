import SwiftUI

/// 三格标签栏：阅读 / 复习 / 设置（决定 29），默认落在第一格（决定 9）。
///
/// **复习那格点进去说「正在开发中」**（决定 30）。做成空屏或者点了没反应都不行——
/// 那看着像坏了；说明白反而清楚。
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
