import Foundation

/// 传输层 the transport
///
/// **Core names this and does not implement it.** 决定 4: the core has to build
/// on Windows, Linux and iOS, and `URLSession` is not the same thing on all
/// three — a core that reached for it would stop being testable in two seconds
/// on the machine it is written on. The host supplies the implementation: the
/// terminal client in this phase, the app in the next.
///
/// The second reason is smaller and still worth having: a core with no network
/// needs no network stub. Every test in this package runs against bytes.
public struct HTTPRequest: Sendable, Equatable {
    /// **加 case 不用动任何实现**:两个传输都用 `method.rawValue` 设方法。
    /// `put` 是 P9 §7 加的——词池快照是**替换**，说对了语义之后重试天然安全。
    public enum Method: String, Sendable {
        case get = "GET", post = "POST", put = "PUT"
    }

    public let method: Method
    /// Path and query, relative to the server's base. Never a full URL — the
    /// base belongs to the host's configuration, not to the core's requests.
    public let path: String
    public let body: Data?
    /// Extra request headers. **Only what the core decides**: authentication
    /// belongs to the host, which knows the token; this is for things like
    /// `If-None-Match`, where the decision is about cached content and so
    /// belongs here.
    public let headers: [String: String]

    public init(method: Method, path: String, body: Data? = nil,
                headers: [String: String] = [:]) {
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers
    }
}

public struct HTTPResponse: Sendable, Equatable {
    public let status: Int
    public let body: Data
    /// Response headers, lowercased keys. Empty when the host does not supply
    /// them — a transport written before this existed keeps working.
    public let headers: [String: String]

    public init(status: Int, body: Data, headers: [String: String] = [:]) {
        self.status = status
        self.body = body
        self.headers = headers
    }

    public var isOK: Bool { (200..<300).contains(status) }

    /// **304 是好消息，不是错误**：服务端说「你那份还是对的」，
    /// 于是这一次传的是零字节，而不是一兆。
    public var isNotModified: Bool { status == 304 }

    public func header(_ name: String) -> String? { headers[name.lowercased()] }
}

/// Why a request did not produce a response.
///
/// **Offline is a first-class answer, not an error to be logged and forgotten.**
/// The whole client is built around the network being absent, so the transport
/// has to say "there is no network" distinctly from "the server said no" — the
/// first means keep the outbox and try later, the second means something is
/// actually wrong.
public enum TransportError: Error, Sendable, Equatable {
    /// No route to the server: radio off, out of range, server not running.
    case offline(String)
    /// A response arrived but the request failed.
    case server(status: Int, body: String)
    /// The response was not what the contract describes.
    case malformed(String)
}

public protocol Transport: Sendable {
    func send(_ request: HTTPRequest) async throws -> HTTPResponse
}
