import Foundation
import ERCore

/// 宿主的那一半 the host's half of the transport.
///
/// Core 只定义协议、不实现网络（P5 决定 4），因为 `URLSession` 在 Windows、
/// Linux 和 iOS 上不是同一样东西，而 Core 三处都要编得过。命令行客户端有它
/// 自己的一份，这里是手机这一份。
///
/// **「离线」必须和「服务器出错」分得开**，否则发件箱会把自己排空到虚无里：
/// 前者的正确反应是留着稍后再发，后者是真的出了事。所以下面把 `URLError`
/// 的一整类都归到 `.offline`——手机上这一类几乎全是网络不在。
struct IOSTransport: Transport {
    let baseURL: URL
    let token: String

    private let session: URLSession
    /// 日志。**这是 `trace_id` 那条链在客户端这头的接点**——服务端把它放进
    /// 错误响应，本来就是为了「用户复制给 AI 就能定位完整链路」，
    /// 而在此之前手机上没有任何地方接得住它。
    private let log: FileLog?

    init(baseURL: URL, token: String, timeout: TimeInterval = 30, log: FileLog? = nil) {
        self.baseURL = baseURL
        self.token = token
        self.log = log

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout * 2
        // 手机上不要「等网回来再发」那套系统重试：这个客户端自己有发件箱，
        // 两套重试叠在一起会让「发出去了没有」这个问题多一个答案。
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
    }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        guard let url = URL(string: request.path, relativeTo: baseURL) else {
            throw TransportError.malformed("拼不出地址：\(request.path)")
        }
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method.rawValue
        urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body = request.body {
            urlRequest.httpBody = body
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }

        let started = Date()
        do {
            let (data, response) = try await session.data(for: urlRequest)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let milliseconds = Int(Date().timeIntervalSince(started) * 1000)

            if (200..<300).contains(status) {
                // 成功那条记 DEBUG：平时不落盘，查一件事的时候才要。
                log?.write(.debug, "http.ok", "请求成功",
                           fields: ["method": request.method.rawValue,
                                    "path": request.path,
                                    "status": String(status),
                                    "ms": String(milliseconds)])
            } else {
                // **失败这条一定要带 trace_id。**服务端的错误响应里就有它，
                // 拿着它去开发者选项的服务端日志里一查，那次请求在服务器上
                // 走过的全程就出来了。
                log?.write(.warn, "http.rejected", "服务端拒收",
                           traceId: Self.trace(in: data),
                           fields: ["method": request.method.rawValue,
                                    "path": request.path,
                                    "status": String(status),
                                    "ms": String(milliseconds)])
            }
            return HTTPResponse(status: status, body: data)
        } catch let error as URLError where error.code == .cancelled {
            // 用户划走了一屏就取消一次请求，这既不是离线也不是故障。
            throw CancellationError()
        } catch {
            // 离线不是错误（Core 那半边也这么看），所以记 INFO 不记 ERROR——
            // 一个把每次进电梯都记成 ERROR 的日志，看的人会学会忽略它。
            log?.write(.info, "http.offline", "联不上",
                       fields: ["path": request.path,
                                "reason": error.localizedDescription])
            throw TransportError.offline(error.localizedDescription)
        }
    }

    /// 从错误响应里取 `trace_id`。**取不到就算了**——请求头一概不记，
    /// 正文也只取这一个字段：日志是要发出去的东西，多带一个字段就多一分
    /// 把不该带的带出去的机会。
    private static func trace(in data: Data) -> String? {
        struct Envelope: Decodable {
            struct Payload: Decodable { let trace_id: String? }
            let error: Payload?
        }
        return try? JSONDecoder().decode(Envelope.self, from: data).error?.trace_id
    }
}
