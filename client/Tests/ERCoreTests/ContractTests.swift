import Foundation
import Testing
import ERContract
@testable import ERCore

/// Does the generated contract decode what the server actually sends?
///
/// The fixtures are whole responses captured from a live backend, not written
/// by hand — 坑 §6.1, because guessing the shape of data someone else produced
/// is how a field ends up parsed by comma and an article reads perfectly while
/// recording nothing.
struct ContractTests {
    static func fixture(_ name: String) throws -> Data {
        let url = Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json")
        #expect(url != nil, "找不到样本 \(name).json")
        return try Data(contentsOf: url!)
    }

    @Test func decodesTheDayPackage() throws {
        let data = try Self.fixture("today")
        let day = try JSONDecoder().decode(Components.Schemas.TodayResponse.self, from: data)

        #expect(day.articles.count == 3, "今日包正好三篇（决定 20）")
        #expect(day.extra_articles.isEmpty, "加餐取消了，但字段必须还在——铁律 5 只增不减")
        #expect(day.excludes_exam_papers, "今日包不含真题，这一点写在响应里而不只在文档里")

        let first = day.articles[0]
        #expect(!(first.article.title.isEmpty))
        #expect((first.tokens?.count ?? 0) > 100, "整篇的 token 都在包里，离线才点得动词")
        #expect((first.glossary?.additionalProperties.count ?? 0) > 50,
                "释义随文章下发，所以离线也能查词")
        #expect(first.preparing == nil, "已就绪的文章不该带 preparing")
    }

    @Test func decodesTheLibrary() throws {
        let list = try JSONDecoder().decode(
            Components.Schemas.LibraryResponse.self, from: try Self.fixture("library"))
        #expect(!list.articles.isEmpty)
        #expect(!list.sortable.additionalProperties.isEmpty,
                "排序方式由服务端给，客户端不要写死")
    }

    @Test func decodesTheReviewDay() throws {
        let day = try JSONDecoder().decode(
            Components.Schemas.ReviewDayResponse.self, from: try Self.fixture("reviews"))
        #expect(day.session_id > 0)
        for item in day.items {
            #expect(item.queue_id > 0, "上报作答用的是排队号，不是义项 id")
            #expect(item.direction == 1 || item.direction == 2)
        }
    }

    @Test func capabilitiesSayWhatIsUnimplemented() throws {
        let day = try JSONDecoder().decode(
            Components.Schemas.TodayResponse.self, from: try Self.fixture("today"))
        // 一个 null 字段本身是歧义的：learner.level 为空，可能是「没数据」，
        // 也可能是「这个功能还没做」。区别写在 capabilities 里。
        #expect(day.capabilities.level_estimate == false)
        #expect(day.learner.level == nil)
    }
}
