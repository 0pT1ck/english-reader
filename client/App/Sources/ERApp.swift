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
    /// **跟着 App 一起诞生，不再跟着标签页**（2026-09-19，用户定的:
    /// 「现在已经颠覆原本的方案，变成本地计算了」）。
    ///
    /// 在这之前它是 `ReviewScreen` 的 `@State`——每次这一格出现才创建，
    /// 从 `phase = .loading` 起步，`.task` 异步把它改成 `.ready`。而这中间
    /// 至少隔着一次 `await`（`SyncEngine` 是 actor，`cachedDay()` 这类调用
    /// 跨执行环境，哪怕内部 0 毫秒跑完也要挂起一次）——SwiftUI 因此**必然**
    /// 先画一帧 `.loading` 再画 `.ready`，跟这一下算得快不快没关系。
    /// 前面几轮把这条路径从「转一整秒」压到「转一帧」，压的是算的时间，
    /// 而这一帧本身是 `.task` 这个机制的调度特性，压不掉。
    ///
    /// 真正的解法是让复习屏出现的那一刻,`model` 已经是 `.ready` 好一阵子了
    /// ——那就不能等复习屏出现才创建它。挪到这里,开屏就预热
    /// （下面的 `.task`），复习屏出现时读到的是一个早就绪的对象,
    /// 不会再经历「诞生于 loading」那一下。
    @State private var reviewModel = ReviewModel()
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
            .environment(reviewModel)
            .task {
                await app.drain()
                // **把复习屏要做的事提前在这里做完，而不是等它出现。**
                // 默认落地的是阅读页，用户点进复习页之前主线程通常闲着一段
                // 时间——趁那段时间把今日包解码、投影重放、日历计算、
                // `reviewModel.phase` 的翻转都做掉。排在 `drain()` 之后:
                // `drain()` 可能拉回别的设备做的事，顺序反过来的话
                // 这里刚预热的缓存会立刻过期。
                await reviewModel.load(app)
            }
            .onChange(of: scenePhase) { _, phase in
                // 回到前台就把攒着的事件发一次。**离线时写下的标记要自己回去**，
                // 不该等用户想起来去点什么。
                if phase == .active {
                    Task {
                        await app.drain()
                        await reviewModel.load(app)
                    }
                }
            }
        }
    }
}
