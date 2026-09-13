import Foundation
import Observation

/// 连接设置 connection settings — base URL 与设备令牌，App 里唯一的配置。
///
/// **令牌和地址分开存，故意的。** 地址进 `UserDefaults`（它不是秘密，而且出问题时
/// 要能一眼看到自己填的是什么）；令牌进钥匙串（它是凭证）。两个都塞进
/// `UserDefaults` 会让备份、日志、调试面板任何一处顺手把令牌带出去。
///
/// **默认地址是空的，不是家里那台机器。** 仓库 2026-09-12 起是公开的，
/// 把内网地址硬编码进源码就是把它发出去。
@MainActor
@Observable
final class Connection {
    private static let baseURLKey = "er.baseURL"

    private(set) var baseURL: String
    private(set) var token: String

    init() {
        baseURL = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? ""
        token = Keychain.read() ?? ""
    }

    var isConfigured: Bool { url != nil && !token.isEmpty }

    /// 拼得出来才算数：一个填错的地址应该在设置页当场看出来，而不是等到
    /// 拉文章时报成「离线」。
    var url: URL? {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed), url.scheme != nil,
              url.host != nil else { return nil }
        return url
    }

    /// 令牌在界面上永远不原样显示，只给够认出「是哪一个」的那几位。
    /// 这跟「API 密钥一律不入日志」是同一条规矩。
    var tokenHint: String {
        guard !token.isEmpty else { return "未填写" }
        guard token.count > 8 else { return String(repeating: "•", count: token.count) }
        return "••••" + token.suffix(4)
    }

    func update(baseURL newBase: String, token newToken: String) {
        baseURL = newBase.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(baseURL, forKey: Self.baseURLKey)

        let trimmedToken = newToken.trimmingCharacters(in: .whitespacesAndNewlines)
        // 空字符串表示「没改」，不是「清空」——设置页里令牌框平时是空的
        // （因为不回显），把空当成清空会让每次改地址都顺手把令牌删掉。
        guard !trimmedToken.isEmpty else { return }
        token = trimmedToken
        Keychain.write(trimmedToken)
    }

    func clearToken() {
        token = ""
        Keychain.delete()
    }
}

/// 钥匙串里就存一条，所以这里不做成通用封装——通用封装会引出「存哪个账户」
/// 这类这个 App 没有的问题。
private enum Keychain {
    private static let service = "com.optick.englishreader"
    private static let account = "device-token"

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func read() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String) {
        guard let data = value.data(using: .utf8) else { return }
        // 先删再加：`SecItemUpdate` 要区分「存在」与「不存在」两条路，
        // 而这里没有并发写，删加两步比分支少一个出错的地方。
        SecItemDelete(baseQuery as CFDictionary)
        var query = baseQuery
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(query as CFDictionary, nil)
    }

    static func delete() {
        SecItemDelete(baseQuery as CFDictionary)
    }
}
