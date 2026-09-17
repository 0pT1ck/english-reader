// swift-tools-version: 6.2
//
// 6.2 而不是 6.0，只为了能写 `.iOS(.v26)`——那是决定 3 在代码里的落点，
// 而 v26 这个符号是 PackageDescription 6.2 才有的。三个环境（本地 6.3.3、
// Linux 容器、Xcode 26）都在我们自己手里，所以提版本没有代价。
import PackageDescription

// **ERCore must stay free of any UI framework.** The rule is in
// `client/README.md` and CI enforces it with a grep; the reason is that this
// package is what an Android client would have to be rewritten from, so
// anything platform-shaped in here is something that gets written twice.
//
// It must also build on Windows, Linux and iOS. That is why networking lives
// in the host (`ercli` here, the app in the next phase) and Core only names
// the protocol: `URLSession` differs across those three, and a Core that
// touched it would stop being testable in two seconds on this machine.
let package = Package(
    name: "ERClient",
    // 只面向 iPhone 17 / iOS 26（决定 3）——一台设备、一个系统版本、一个使用者，
    // 不背任何向下兼容。macOS 那条是 CI 上跑测试要的。
    platforms: [.iOS(.v26), .macOS(.v15)],
    products: [
        .library(name: "ERCore", targets: ["ERCore"]),
        .executable(name: "ercli", targets: ["ercli"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-openapi-runtime", from: "1.0.0"),
        // FSRS，P9 起（phase-9.html §14 U1）。**官方那个**——和服务端用的
        // `py-fsrs` 同一个组织（open-spaced-repetition），MIT 许可。
        //
        // 选它而不是自己写，理由里最要紧的一条是**它自己没有依赖**：
        // 上面那段注释说 Core 必须能在 Windows、Linux、iOS 上都编得过，
        // 而 P5 定的规矩是「Core 的每个依赖都要单独核实能不能编」——
        // 一个零依赖的包只要核它一个。它也已经开着
        // `StrictConcurrency=complete`，和我们同一个规矩。
        .package(url: "https://github.com/open-spaced-repetition/swift-fsrs", from: "5.0.0"),
    ],
    targets: [
        // Generated from `client/openapi.json` by
        // `scripts/generate_swift_types.py`. Committed, never hand-edited: CI
        // regenerates and fails on a diff, so an edit here would be reverted
        // by the next run anyway.
        .target(
            name: "ERContract",
            dependencies: [.product(name: "OpenAPIRuntime", package: "swift-openapi-runtime")]
        ),
        .target(
            name: "ERCore",
            dependencies: [
                "ERContract",
                // 产品名是 `FSRS`，包名是 `swift-fsrs`——两个不一样，
                // 写错了报的错是「找不到这个产品」。
                .product(name: "FSRS", package: "swift-fsrs"),
            ]
        ),
        .executableTarget(name: "ercli", dependencies: ["ERCore"]),
        .testTarget(
            name: "ERCoreTests",
            dependencies: ["ERCore"],
            // The fixtures are real server responses and the review vectors
            // come out of the server's own state machine. Both are read at
            // test time rather than compiled in, so refreshing them is a file
            // copy rather than a rebuild.
            resources: [.copy("Fixtures")]
        ),
    ]
)
