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
    private static let adminURLKey = "er.adminBaseURL"

    private(set) var baseURL: String
    private(set) var token: String

    /// 管理密码。**第二把凭证，和设备令牌分开存、分开显示、分开清除**（决定 27）。
    ///
    /// 开发者选项直接控制后台（决定 25），而后端的 `require_admin` 本来就接受
    /// `X-Admin-Secret` 请求头。两把凭证在一个 App 里，**界面上必须分得开**——
    /// 把管理密码填进令牌那一栏的话，到处都是 401 而看不出原因。
    ///
    /// 手机丢了它就跟着丢，而它和设备令牌的补救方式不一样：令牌能在管理台单独
    /// 吊销，密码只有「改 `ER_ADMIN_SECRET` 然后重启」一条路。
    private(set) var adminSecret: String

    /// 管理接口的地址。**默认跟着服务器地址走**（同一台机器），
    /// 允许单独填——开发期实例和主副本确实可能不是一个地址（决定 29）。
    private(set) var adminBaseURLOverride: String

    init() {
        baseURL = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? ""
        token = Keychain.read(.deviceToken) ?? ""
        adminSecret = Keychain.read(.adminSecret) ?? ""
        adminBaseURLOverride = UserDefaults.standard.string(forKey: Self.adminURLKey) ?? ""
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
    var tokenHint: String { Self.hint(token) }
    var adminSecretHint: String { Self.hint(adminSecret) }

    private static func hint(_ value: String) -> String {
        guard !value.isEmpty else { return "未填写" }
        guard value.count > 8 else { return String(repeating: "•", count: value.count) }
        return "••••" + value.suffix(4)
    }

    /// 管理接口的 base。没单独填就用服务器那个——同一台机器是常态，
    /// 而要求填两遍只会让人把其中一个填错。
    var adminURL: URL? {
        let override = adminBaseURLOverride.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !override.isEmpty else { return url }
        guard let parsed = URL(string: override), parsed.scheme != nil,
              parsed.host != nil else { return nil }
        return parsed
    }

    var isAdminConfigured: Bool { adminURL != nil && !adminSecret.isEmpty }

    func update(baseURL newBase: String, token newToken: String) {
        baseURL = newBase.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(baseURL, forKey: Self.baseURLKey)

        let trimmedToken = newToken.trimmingCharacters(in: .whitespacesAndNewlines)
        // 空字符串表示「没改」，不是「清空」——设置页里令牌框平时是空的
        // （因为不回显），把空当成清空会让每次改地址都顺手把令牌删掉。
        guard !trimmedToken.isEmpty else { return }
        token = trimmedToken
        Keychain.write(trimmedToken, .deviceToken)
    }

    func clearToken() {
        token = ""
        Keychain.delete(.deviceToken)
    }

    /// 空字符串表示「没改」，同 `update(baseURL:token:)` 的规矩。
    func updateAdmin(secret newSecret: String, baseURL newBase: String) {
        adminBaseURLOverride = newBase.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(adminBaseURLOverride, forKey: Self.adminURLKey)

        let trimmed = newSecret.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        adminSecret = trimmed
        Keychain.write(trimmed, .adminSecret)
    }

    func clearAdminSecret() {
        adminSecret = ""
        Keychain.delete(.adminSecret)
    }
}

/// 钥匙串里存两条：设备令牌，和管理密码（2026-09-15 加）。
///
/// **两条而不是一条通用封装。**`Account` 是个封闭的枚举，不是字符串参数——
/// 一个拿字符串当账户名的封装，迟早会在某处把 `"admin-secret"` 拼错成
/// `"admin_secret"`，然后安静地读出一个空值，而空值在这里的意思是「没填过」。
private enum Keychain {
    private static let service = "com.optick.englishreader"

    enum Account: String {
        case deviceToken = "device-token"
        case adminSecret = "admin-secret"
    }

    private static func baseQuery(_ account: Account) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account.rawValue]
    }

    static func read(_ account: Account) -> String? {
        var query = baseQuery(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String, _ account: Account) {
        guard let data = value.data(using: .utf8) else { return }
        // 先删再加：`SecItemUpdate` 要区分「存在」与「不存在」两条路，
        // 而这里没有并发写，删加两步比分支少一个出错的地方。
        SecItemDelete(baseQuery(account) as CFDictionary)
        var query = baseQuery(account)
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(query as CFDictionary, nil)
    }

    static func delete(_ account: Account) {
        SecItemDelete(baseQuery(account) as CFDictionary)
    }
}
