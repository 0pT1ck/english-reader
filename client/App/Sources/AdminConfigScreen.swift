import SwiftUI
import ERCore

/// 配置热改（P8 §8 决定 26 的第三样）。
///
/// **这是「每天几篇」真正能改的地方。**设置页那一区只读（§7），
/// 而这里能改任何一项运行参数——它们不是偏好，是运行配置，
/// 所以它们该待在开发者选项后面，不该摆在正常路径上。
struct AdminConfigScreen: View {
    @Environment(AppModel.self) private var app

    @State private var specs: [AdminClient.ConfigSpec] = []
    @State private var editing: AdminClient.ConfigSpec?
    @State private var draft = ""
    @State private var failure: String?
    @State private var search = ""

    var body: some View {
        List {
            if let failure {
                Section { Text(failure).foregroundStyle(.red).font(.footnote) }
            }
            ForEach(groups, id: \.name) { group in
                Section(group.name) {
                    ForEach(group.specs) { spec in
                        row(spec)
                    }
                }
            }
        }
        .navigationTitle("配置")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $search, prompt: "找一项配置")
        .task { await load() }
        .sheet(item: $editing) { spec in
            editor(spec)
        }
    }

    private struct Group {
        let name: String
        let specs: [AdminClient.ConfigSpec]
    }

    private var groups: [Group] {
        let filtered = search.isEmpty ? specs : specs.filter {
            $0.key.localizedCaseInsensitiveContains(search)
                || $0.title.localizedCaseInsensitiveContains(search)
        }
        return Dictionary(grouping: filtered, by: \.group)
            .map { Group(name: $0.key, specs: $0.value.sorted { $0.key < $1.key }) }
            .sorted { $0.name < $1.name }
    }

    @ViewBuilder
    private func row(_ spec: AdminClient.ConfigSpec) -> some View {
        // 布尔值就地拨，别的进编辑面板：一个开关不值得为它开一张纸。
        if case .bool(let on)? = spec.value {
            Toggle(isOn: Binding(
                get: { on },
                set: { newValue in Task { await save(spec, .bool(newValue)) } }
            )) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(spec.title)
                    Text(spec.key).font(.caption2.monospaced()).foregroundStyle(.tertiary)
                }
            }
        } else {
            Button {
                draft = Self.text(spec.value)
                editing = spec
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(spec.title).foregroundStyle(.primary)
                        Text(spec.key).font(.caption2.monospaced())
                            .foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Text(Self.text(spec.value))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
    }

    private func editor(_ spec: AdminClient.ConfigSpec) -> some View {
        NavigationStack {
            Form {
                Section {
                    TextField(spec.value_type, text: $draft, axis: .vertical)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .lineLimit(1...6)
                } header: {
                    Text(spec.key).font(.caption.monospaced())
                } footer: {
                    Text("类型 \(spec.value_type)。填错了服务端会拒绝，"
                         + "并且说清楚哪里不对。")
                }
            }
            .navigationTitle(spec.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { editing = nil }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        Task {
                            await save(spec, Self.parse(draft, as: spec.value_type))
                            editing = nil
                        }
                    }
                }
            }
        }
    }

    private func load() async {
        guard let admin = app.admin else { return }
        do {
            specs = try await admin.config().specs
            failure = nil
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    private func save(_ spec: AdminClient.ConfigSpec, _ value: JSONValue) async {
        guard let admin = app.admin else { return }
        do {
            _ = try await admin.setConfig(spec.key, value: value)
            app.log?.write(.info, "admin.config.set", "改了一项配置",
                           fields: ["key": spec.key])
            await load()
        } catch {
            failure = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    /// 服务端给的是任意 JSON，屏幕上要的是一行字。
    static func text(_ value: JSONValue?) -> String {
        guard let value else { return "—" }
        switch value {
        case .string(let text): return text
        case .int(let number): return String(number)
        case .double(let number): return String(number)
        case .bool(let flag): return flag ? "true" : "false"
        case .null: return "—"
        case .array, .object:
            guard let data = try? JSONEncoder().encode(value),
                  let text = String(data: data, encoding: .utf8) else { return "…" }
            return text
        }
    }

    /// **按服务端声明的类型转，不靠猜。**一个把 `3` 发成 `"3"` 的客户端，
    /// 会让配置表里悄悄多出一个字符串，而读它的地方要到下一次运行才发作。
    static func parse(_ text: String, as type: String) -> JSONValue {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        switch type {
        case "int":
            return Int(trimmed).map { JSONValue.int($0) } ?? .string(trimmed)
        case "float", "number":
            return Double(trimmed).map { JSONValue.double($0) } ?? .string(trimmed)
        case "bool":
            return .bool(["true", "1", "yes", "on"].contains(trimmed.lowercased()))
        case "json":
            guard let data = trimmed.data(using: .utf8),
                  let value = try? JSONDecoder().decode(JSONValue.self, from: data) else {
                return .string(trimmed)
            }
            return value
        default:
            return .string(trimmed)
        }
    }
}
