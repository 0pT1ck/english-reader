import Foundation
import ERCore

/// 开发者选项用的管理端客户端 the admin client behind developer options
///
/// **这条路是 2026-09-15 定的**：开发者选项直接控制后台，不内嵌管理台网页——
/// 那些页面是给鼠标设计的，表格宽、按钮小，手机上点不动。
///
/// **后端一行都没改**：`core/auth.py` 的 `require_admin` 本来就接受
/// `X-Admin-Secret` 请求头，注释写的是「命令行，以及 AI 直接查诊断时用」。
/// 「非浏览器的客户端拿着 secret 直接调」这条路一直在，只是以前那个客户端
/// 是命令行。
///
/// **这里只有五样东西**（决定 26）：跳天、任务、配置、日志、状态。判据是
/// 「验收与开发期天天要用」——别的一律回管理台。这条判据写进代码注释里，
/// 是因为「顺便再加一个」的门槛一旦没了，这个文件就会长成第二个管理台。
///
/// **凭证不入日志。**这个类是唯一持有管理密码的地方，它不把密码放进任何
/// 错误消息——错误消息是会被写进客户端日志、然后被导出分享的东西。
struct AdminClient {
    let baseURL: URL
    let secret: String

    enum Failure: LocalizedError {
        case unauthorized
        case http(Int, String)
        case offline(String)
        case malformed

        var errorDescription: String? {
            switch self {
            case .unauthorized: return "管理密码不对（401）"
            case .http(let code, let body):
                return "服务端回了 \(code)：\(body.prefix(200))"
            case .offline(let reason): return "连不上：\(reason)"
            case .malformed: return "回来的内容看不懂——可能不是这个服务"
            }
        }
    }

    // MARK: 五样

    func clock() async throws -> ClockStatus {
        try await get("review/clock")
    }

    /// 跳一天，或者归零。**归零是要有的**：在模拟的未来里通过验收什么也证明不了，
    /// 而忘了归零的下一次验收会拿着错的「今天」跑。
    @discardableResult
    func advanceClock(days: Int?) async throws -> ClockStatus {
        let body: [String: JSONValue] = days.map { ["days": .int($0)] } ?? ["reset": .bool(true)]
        return try await send("review/clock", method: "POST", body: body)
    }

    func tasks() async throws -> TaskList {
        try await get("tasks")
    }

    /// **同步跑，慢是有意的。**服务端那一侧就是在请求线程里跑的：这些活要几分钟，
    /// 而一个立刻返回、然后安静失败的按钮正是这个项目反复踩到的形状。
    func runTask(_ name: String) async throws -> JSONValue {
        try await send("tasks/\(name)/run", method: "POST", body: [:])
    }

    func config() async throws -> ConfigList {
        try await get("config")
    }

    func setConfig(_ key: String, value: JSONValue) async throws -> JSONValue {
        try await send("config/\(key)", method: "PUT", body: ["value": value])
    }

    /// 查服务端日志。`traceId` 是最要紧的那个参数——手机上拿到一个 trace，
    /// 用它就能把服务器那半边的完整链路捞出来。
    func logs(level: String? = nil, traceId: String? = nil, limit: Int = 100)
        async throws -> LogList {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let level { query.append(URLQueryItem(name: "level", value: level)) }
        if let traceId { query.append(URLQueryItem(name: "trace_id", value: traceId)) }
        return try await get("logs", query: query)
    }

    func status() async throws -> ServerStatus {
        try await get("status")
    }

    // MARK: 返回的形状
    //
    // 管理接口不进契约（那是 60 多个手机永远不会调的端点，每一个都会变成
    // 没人用的 Swift 类型），所以这几个结构是手写的，且**只取用得着的字段**。
    // 后端加字段不影响这里；改字段名会解码失败，而失败会显示出来——
    // 对一个开发工具，这个代价是对的。

    struct ClockStatus: Decodable {
        let offset_days: Int
        let simulated_now: String
        let simulated: Bool
    }

    struct TaskList: Decodable {
        let tick_seconds: Int
        let tasks: [TaskRow]
    }

    struct TaskRow: Decodable, Identifiable {
        let name: String
        let title: String
        let schedule: String
        let enabled: Bool
        let running: Bool
        let next_due_at: String?
        let last_status: String?
        let last_error: String?
        var id: String { name }
    }

    struct ConfigList: Decodable {
        let specs: [ConfigSpec]
    }

    struct ConfigSpec: Decodable, Identifiable {
        let key: String
        let title: String
        let group: String
        let value_type: String
        let value: JSONValue?
        var id: String { key }
    }

    /// 服务状态里**只取一眼能看出「有没有事」的那几样**：几个库多大、
    /// 过去一天各级别日志多少、装了哪些模块。别的（事件订阅表、设备清单、
    /// 待恢复的备份）在手机上帮不上忙，而且它们会把这一屏挤成一张表格。
    struct ServerStatus: Decodable {
        let version: String
        let time: String
        let dev_mode: Bool
        let databases: [String: Database]
        let modules: [Module]
        let logs_last_24h: [String: Int]

        struct Database: Decodable {
            let exists: Bool
            let size_bytes: Int
        }

        struct Module: Decodable, Identifiable {
            let name: String
            let title: String
            var id: String { name }
        }
    }

    struct LogList: Decodable {
        let count: Int
        let records: [LogRow]
    }

    struct LogRow: Decodable, Identifiable {
        let id: Int
        let ts: String
        let level: String
        let module: String
        let event: String
        let message: String
        let trace_id: String?
    }

    // MARK: 传输

    /// 调用处写的是管理接口上那一段（`tasks`、`review/clock`），前缀只在这里拼一次。
    private func url(_ path: String, query: [URLQueryItem] = []) -> URL? {
        var resolved = baseURL.appendingPathComponent("v1/admin")
        for segment in path.split(separator: "/") {
            resolved = resolved.appendingPathComponent(String(segment))
        }
        guard var components = URLComponents(url: resolved, resolvingAgainstBaseURL: false)
        else { return nil }
        if !query.isEmpty { components.queryItems = query }
        return components.url
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await perform(path, method: "GET", query: query, body: nil)
    }

    private func send<T: Decodable>(_ path: String, method: String,
                                    body: [String: JSONValue]) async throws -> T {
        try await perform(path, method: method, query: [], body: body)
    }

    private func perform<T: Decodable>(_ path: String, method: String,
                                       query: [URLQueryItem],
                                       body: [String: JSONValue]?) async throws -> T {
        guard let url = url(path, query: query) else { throw Failure.malformed }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(secret, forHTTPHeaderField: "X-Admin-Secret")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONEncoder().encode(body)
        }
        // 一次手动触发的生成任务要跑几分钟，而默认 60 秒会在中途断开——
        // 断开之后任务还在服务端跑着，屏幕上却是一个错误。
        request.timeoutInterval = 300

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw Failure.offline(error.localizedDescription)
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 || code == 403 { throw Failure.unauthorized }
        guard (200..<300).contains(code) else {
            throw Failure.http(code, String(data: data, encoding: .utf8) ?? "")
        }
        guard let decoded = try? JSONDecoder().decode(T.self, from: data) else {
            throw Failure.malformed
        }
        return decoded
    }
}
