import SwiftUI
import ERCore
import ERContract

/// 第一屏：复习主界面。日历 + 两个池子的入口。
struct ReviewScreen: View {
    @Environment(AppModel.self) private var app
    /// **不再是这一屏自己的 `@State`**（2026-09-19）。`ReviewModel` 现在跟着
    /// `EnglishReaderApp` 一起创建、开屏就预热——这一屏出现时读到的通常已经
    /// 是一个早就绪的对象，不会再经历「诞生于 `.loading`」那一下。
    /// 见 `ERApp.swift` 顶部那段注释。
    @Environment(ReviewModel.self) private var model

    var body: some View {
        NavigationStack {
            content
                .reviewBackground()
                .navigationTitle("复习")
                .navigationDestination(isPresented: sessionBinding) {
                    ReviewSessionScreen(model: model)
                }
        }
        // **两处触发，不是一处，这一条依然成立。**`model` 虽然不再跟着这一屏
        // 生灭，但开屏预热可能还没跑完、或者预热时恰好离线——那时 `phase`
        // 仍然是 `.loading`，这两处入口负责把它补上。稳态下（预热早就做完）
        // `needsLoad` 是假，这两行什么都不做，也就没有那一帧可画。
        //
        // `.task` 在视图出现时跑，但它会在视图消失（切走一格、被重算）时
        // 取消——而取消之后没有任何东西会再叫它一次，所以 `.onAppear` 兜底。
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
                        model.begin(bucket: .today)
                    }
                    PoolCard(title: "温故知新", done: model.dueDone, total: model.dueTotal) {
                        model.begin(bucket: .due)
                    }
                    // 拼写是**可选强化**（P3 决定 13），所以它不是第三张卡片：
                    // 卡片是「今天要做的事」，而这一行只在两个池子都走完之后
                    // 才出现，出现了也可以不理。
                    // **投影说该问、而包里还没有它的句子。** 平时是 0；
                    // 非零的情形是真实的:你离线标了个词，包还没重新取下来。
                    // 那个词没丢（投影里它在），只是还问不了——
                    // 不说出来的话，「今天 12 个」和「13 个」的差别没人解释得了。
                    if model.awaitingContent > 0 {
                        Text("还有 \(model.awaitingContent) 个词等着句子备好，"
                             + "联网取一次今日包就会出现")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }

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
/// **颜色是 `ReviewCalendar` 算的，这里只上色。**「那天算不算完成」是一条规则，
/// 而它有个容易想错的地方：**系统没派活的日子不该判成你失败**，
/// 所以那种日子是 partial 而不是 missed。
///
/// P9 之前这条规则在服务端（`/reviews/calendar`），现在在设备上——
/// 同一条规则，换了执行的地方，所以答完一题当场变色而不是等下一次联网。
private struct CalendarStrip: View {
    let days: [ReviewCalendar.Day]

    var body: some View {
        HStack(spacing: 0) {
            ForEach(days, id: \.day) { day in
                VStack(spacing: 6) {
                    Text(number(day.day))
                        .font(day.isToday ? .headline : .body)
                        .foregroundStyle(day.isToday ? Color.white : .primary)
                        .frame(width: 34, height: 34)
                        .background(day.isToday ? Color.primary : .clear, in: .circle)
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

    private func colour(_ day: ReviewCalendar.Day) -> Color {
        switch day.status {
        case .complete: .green
        case .partial: .gray
        case .missed: .red
        case .unknown: .clear    // 那天没开过 App，重放不出来
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
