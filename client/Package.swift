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
        .target(name: "ERCore", dependencies: ["ERContract"]),
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
