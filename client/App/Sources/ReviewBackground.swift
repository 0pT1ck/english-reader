import SwiftUI

/// 复习那一格的背景图。
///
/// **为什么做成一个 modifier，而不是在一处贴一张图**：复习是一条
/// `NavigationStack`，主界面、作答、拼写是三个各自渲染的视图——只在主界面贴的话，
/// 一点「开始复习」就闪回系统白底。三屏各调一次，这条链上才没有缝。
///
/// **图是浅色的，而 App 跟随系统外观**（没有设 `preferredColorScheme`）。
/// 深色下 `.primary` 是白字，白字落在这张浅图上读不出来——所以深色时
/// 在图上压一层黑。压暗保的是「字读得出来」，不是好看；真要深色下也用原图，
/// 得另配一张深色版，那是换素材的事，不是改代码的事。
struct ReviewBackground: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Color.clear
            .overlay {
                Image("ReviewBackground")
                    .resizable()
                    .scaledToFill()
            }
            .overlay {
                // 闭包形式不是风格选择：`Color` 同时是 View 和 ShapeStyle，
                // `.overlay(someColor)` 在两个重载之间是歧义的。
                if scheme == .dark { Color.black.opacity(0.62) }
            }
            // 先裁再让出安全区：`scaledToFill` 会溢出自己的框，不裁的话
            // 溢出的那部分照样参与布局。
            .clipped()
            .ignoresSafeArea()
            // 它垫在滚动区域底下，手势要能穿过去。
            .allowsHitTesting(false)
    }
}

extension View {
    /// 把复习那一格的背景垫在这一屏下面。
    func reviewBackground() -> some View {
        background { ReviewBackground() }
    }
}
