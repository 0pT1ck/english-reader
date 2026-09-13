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

    init(baseURL: URL, token: String, timeout: TimeInterval = 30) {
        self.baseURL = baseURL
        self.token = token

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

        do {
            let (data, response) = try await session.data(for: urlRequest)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            return HTTPResponse(status: status, body: data)
        } catch let error as URLError where error.code == .cancelled {
            // 用户划走了一屏就取消一次请求，这既不是离线也不是故障。
            throw CancellationError()
        } catch {
            throw TransportError.offline(error.localizedDescription)
        }
    }
}
