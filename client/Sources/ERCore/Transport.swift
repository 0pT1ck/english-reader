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
    /// 服务端说这台设备的契约版本太旧（HTTP 426）。**不要重试**：
    /// 它在 App 更新之前不会变，重试就是 P9 §17 那个一直转的圈。
    case upgradeRequired(String)
}

public protocol Transport: Sendable {
    func send(_ request: HTTPRequest) async throws -> HTTPResponse
}

// MARK: - 契约版本 contract version

/// 契约版本 contract version / 版本握手 version handshake
///
/// **What this replaces.** Until 2026-09-22 the API was additive for ever
/// (架构铁律 7), and the reason given was that a sideloaded client updates late.
/// The user retired that premise — clients are required to be current — which
/// makes fields deletable and meanings changeable, and makes *this* necessary:
/// a requirement nothing enforces is a wish, and a stale client reading a field
/// whose meaning moved shows wrong content **without an error anywhere**.
///
/// **Why the number lives in Core rather than in the host.** The host knows the
/// token; Core knows which contract it was generated against (`ERContract` is
/// built from `client/openapi.json`). The app's own build number answers a
/// different question and `ercli` does not have one at all.
public enum ContractVersion {
    /// Bumped whenever the server's contract changes in a way a client must
    /// follow. It is compared against the server's configured minimum; equal or
    /// higher passes.
    ///
    /// **2 是 P11**：词组带上了义项、`translation`/`definition` 删掉了、
    /// 义项多了搭配。1 的客户端读不出词组的意思，所以服务端该把它挡在外面。
    public static let current = 2
    public static let header = "X-Contract-Version"
}

/// Wraps another transport and stamps every request with the contract version.
///
/// **A wrapper rather than a line in each call site**, because "every request"
/// is the requirement: one forgotten call is one path the server cannot tell is
/// out of date, and that path is the one that will be wrong.
public struct VersionedTransport: Transport {
    private let inner: any Transport

    public init(_ inner: any Transport) { self.inner = inner }

    public func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        var headers = request.headers
        headers[ContractVersion.header] = String(ContractVersion.current)
        let response = try await inner.send(
            HTTPRequest(method: request.method, path: request.path,
                        body: request.body, headers: headers))
        // **426 不是一次普通的失败，重试它只会一直转圈**（P9 §17 的形状）。
        // 它是个终局答案：这台设备的版本太旧，等下一次 App 更新之前不会变。
        // 抛一个能认出来的错，让调用方停下同步、把话说给使用者听，
        // **而发件箱里攒着的事件一条都不许丢**——更新完照样要上报。
        if response.status == 426 {
            throw TransportError.upgradeRequired(
                String(data: response.body, encoding: .utf8) ?? "")
        }
        return response
    }
}
