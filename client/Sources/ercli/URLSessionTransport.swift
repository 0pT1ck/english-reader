import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import ERCore

/// The host's half of the transport. Core names the protocol and stops there
/// (决定 4), because `URLSession` is not the same thing on Windows, Linux and
/// iOS — and the core has to build on all three.
///
/// **Offline has to be told apart from a server error**, or the outbox drains
/// itself into the void. Two things on this machine make that harder than it
/// sounds, both measured on 2026-09-12:
///
/// * Without `NO_PROXY`, a request to `127.0.0.1` goes through the local proxy,
///   which answers **503 for a port nothing is listening on**. "Connection
///   refused" arrives looking like "the server is unwell", and a client would
///   report the failure instead of queueing the work.
/// * Setting both `NO_PROXY` and `no_proxy` is a **fatal error before any of
///   our code runs**: Windows environment variables are case-insensitive and
///   Foundation builds a dictionary out of them.
///
/// So this configures the session's proxy behaviour itself rather than trusting
/// whatever the shell happened to export.
struct URLSessionTransport: Transport {
    let baseURL: URL
    let token: String
    let timeout: TimeInterval

    private let session: URLSession

    init(baseURL: URL, token: String, timeout: TimeInterval = 60) {
        self.baseURL = baseURL
        self.token = token
        self.timeout = timeout

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        // Belt and braces: the environment is also fixed up in `main`, but a
        // session that says "no proxy" cannot be undone by a stray variable.
        configuration.connectionProxyDictionary = [:]
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
        // Core 自己要带的头（现在只有 `If-None-Match`）。认证不在这里——
        // 那是宿主的事，它才知道令牌。
        for (name, value) in request.headers {
            urlRequest.setValue(value, forHTTPHeaderField: name)
        }

        do {
            let (data, response) = try await session.data(for: urlRequest)
            let http = response as? HTTPURLResponse
            let status = http?.statusCode ?? 0
            // 头的键统一小写：`URLSession` 不保证大小写，而 Core 按小写查。
            var headers: [String: String] = [:]
            for (key, value) in http?.allHeaderFields ?? [:] {
                if let name = key as? String, let text = value as? String {
                    headers[name.lowercased()] = text
                }
            }
            return HTTPResponse(status: status, body: data, headers: headers)
        } catch let error as URLError {
            switch error.code {
            case .notConnectedToInternet, .cannotConnectToHost, .cannotFindHost,
                 .networkConnectionLost, .timedOut, .dnsLookupFailed:
                throw TransportError.offline(error.localizedDescription)
            default:
                throw TransportError.offline(error.localizedDescription)
            }
        } catch {
            throw TransportError.offline("\(error)")
        }
    }
}

/// A transport that refuses everything, for walking a day with the network
/// deliberately gone.
///
/// Its own type rather than a flag inside the real one: pretending to be
/// offline by returning errors from a live session leaves the door open to
/// accidentally succeeding, and the whole point of the exercise is that the
/// door is shut.
struct OfflineTransport: Transport {
    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        throw TransportError.offline("离线模式：这一次故意不连服务器")
    }
}

/// Environment repair, run once at startup.
///
/// Not paranoia — see the two measured failures above. Doing it here means a
/// shell with either problem still produces a working client, and the person
/// running it never has to know.
enum ProxyEnvironment {
    /// Report the environment that gives wrong answers, so whoever is running
    /// this is told rather than left to work it out.
    ///
    /// **Repairing it from inside is not possible here.** Windows has no
    /// `setenv`, and by the time any of our code runs Foundation has already
    /// built its environment dictionary — the duplicate-key crash from having
    /// both `NO_PROXY` and `no_proxy` happens before `main`, so a process that
    /// reaches this line survived that one. What is left to warn about is the
    /// other problem, the one that produces wrong answers rather than none:
    /// without `NO_PROXY`, a request to `127.0.0.1` goes through the proxy,
    /// which answers **503 for a port nothing is listening on**. "Offline"
    /// arrives looking like "the server is unwell", and the two are handled in
    /// opposite ways.
    static func warning() -> String? {
        let environment = ProcessInfo.processInfo.environment
        let hasProxy = environment["HTTP_PROXY"] != nil || environment["http_proxy"] != nil
        let hasNoProxy = environment["NO_PROXY"] != nil || environment["no_proxy"] != nil
        guard hasProxy, !hasNoProxy else { return nil }
        return """
        注意：设了 HTTP_PROXY 但没设 NO_PROXY。
          这个客户端自己把会话级代理关掉了，所以它没事；但同一个终端里跑别的东西会撞上——
          「连不上」会被代理翻译成 503，而「网断了」和「服务器出错了」要反着处理。
          设成：NO_PROXY=localhost,127.0.0.1,::1（**只设大写那个**，两个都设会让
          Swift 程序在 main 之前就崩掉）
        """
    }
}
