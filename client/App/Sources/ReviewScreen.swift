import SwiftUI
import ERCore
import ERContract

/// 第一屏：复习主界面。日历 + 两个池子的入口。
struct ReviewScreen: View {
    @Environment(AppModel.self) private var app
    @State private var model = ReviewModel()

    var body: some View {
        NavigationStack {
            content
                .reviewBackground()
                .navigationTitle("复习")
                .navigationDestination(isPresented: sessionBinding) {
                    ReviewSessionScreen(model: model)
                }
        }
        // **两处触发，不是一处。**`.task` 在视图出现时跑，但它会在视图消失
        // （切走一格、被重算）时取消——而取消之后没有任何东西会再叫它一次。
        // 真机上第一次进复习就撞到了这个：包还没拉完 task 就没了。
        .task { if model.needsLoad { await model.load(app) } }
        .onAppear { if model.needsLoad { Task { await model.load(app) } } }
    }

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .offline(let text):
            // 转圈继续转，旁边写清楚原因（同 P6 决定 33）。
            VStack(spacing: 14) {
                ProgressView()
                Text(text).font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .task { await retry() }
        case .failed(let text):
            ContentUnavailableView("复习打不开", systemImage: "exclamationmark.triangle",
                                   description: Text(text))
        case .ready:
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    CalendarStrip(days: model.days)
                    if model.streak > 0 {
                        Text("你已经连续打卡了 \(model.streak) 天！")
                            .font(.headline)
                    }
                    PoolCard(title: "今日学习", done: model.todayDone, total: model.todayTotal) {
                        model.begin(bucket: "today")
                    }
                    PoolCard(title: "温故知新", done: model.dueDone, total: model.dueTotal) {
                        model.begin(bucket: "due")
                    }
                    // 拼写是**可选强化**（P3 决定 13），所以它不是第三张卡片：
                    // 卡片是「今天要做的事」，而这一行只在两个池子都走完之后
                    // 才出现，出现了也可以不理。
                    if model.spellingEnabled && model.spellingAvailable
                        && !model.spellingWords.isEmpty {
                        NavigationLink {
                            SpellingScreen(words: model.spellingWords)
                        } label: {
                            Label("拼写强化 · \(model.spellingWords.count) 个词",
                                  systemImage: "keyboard")
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.vertical, 6)
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 40)
            }
            .refreshable { await model.load(app) }
        }
    }

    private var sessionBinding: Binding<Bool> {
        Binding(get: { model.bucket != nil }, set: { if !$0 { model.leave() } })
    }

    private func retry() async {
        while case .offline = model.phase {
            try? await Task.sleep(for: .seconds(5))
            if Task.isCancelled { return }
            await model.load(app)
        }
    }
}

/// 七天打卡条。
///
/// **颜色是服务端算的，这里只上色。**「那天算不算完成」是一条规则
/// （架构铁律 1），而且它有个容易想错的地方：**系统没派活的日子不该判成你失败**，
/// 所以那种日子回的是 partial 而不是 missed。
private struct CalendarStrip: View {
    let days: [Components.Schemas.CalendarDay]

    var body: some View {
        HStack(spacing: 0) {
            ForEach(days, id: \.day) { day in
                VStack(spacing: 6) {
                    Text(number(day.day))
                        .font(day.is_today ? .headline : .body)
                        .foregroundStyle(day.is_today ? Color.white : .primary)
                        .frame(width: 34, height: 34)
                        .background(day.is_today ? Color.primary : .clear, in: .circle)
                    Circle()
                        .fill(colour(day))
                        .frame(width: 6, height: 6)
                        // 未来的日子不上色（决定 4）。占位保留，免得整条抖动。
                        .opacity(colour(day) == .clear ? 0 : 1)
                }
                .frame(maxWidth: .infinity)
            }
        }
    }

    private func number(_ iso: String) -> String {
        String(iso.suffix(2)).hasPrefix("0")
            ? String(iso.suffix(1)) : String(iso.suffix(2))
    }

    private func colour(_ day: Components.Schemas.CalendarDay) -> Color {
        switch day.status {
        case "complete": .green
        case "partial": .gray
        case "missed": .red
        default: .clear          // unknown：那天没开过 App，重建不出来
        }
    }
}

/// 一个池子的卡片。
private struct PoolCard: View {
    let title: String
    let done: Int
    let total: Int
    let start: () -> Void

    private var finished: Bool { total > 0 && done >= total }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                Text(title).font(.title3.weight(.semibold))
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    // 「已复习/共」——不写「已复习/待复习」，那样 30 该是 23（决定 2）。
                    Text("已复习/共").font(.caption).foregroundStyle(.secondary)
                    Text("\(done)/\(total)").font(.title3.monospacedDigit())
                }
            }
            HStack {
                Spacer()
                if finished {
                    Label("已完成", systemImage: "checkmark")
                        .font(.subheadline)
                        .foregroundStyle(.green)
                } else {
                    Button("开始复习", action: start)
                        .buttonStyle(.glassProminent)
                        .disabled(total - done <= 0)
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.fill.tertiary, in: .rect(cornerRadius: 20))
    }
}
