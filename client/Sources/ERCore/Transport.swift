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
    public enum Method: String, Sendable { case get = "GET", post = "POST" }

    public let method: Method
    /// Path and query, relative to the server's base. Never a full URL — the
    /// base belongs to the host's configuration, not to the core's requests.
    public let path: String
    public let body: Data?

    public init(method: Method, path: String, body: Data? = nil) {
        self.method = method
        self.path = path
        self.body = body
    }
}

public struct HTTPResponse: Sendable, Equatable {
    public let status: Int
    public let body: Data

    public init(status: Int, body: Data) {
        self.status = status
        self.body = body
    }

    public var isOK: Bool { (200..<300).contains(status) }
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
