# 客户端

P5 的产物：一个纯 Swift 的内核，和一个能把它真正走一遍的命令行客户端。
界面不在这里——它是下一个 Phase 的事（`docs/phase-5.html` §1、§13）。

| 目录 | 装什么 | 谁写的 |
|---|---|---|
| `Sources/ERContract` | 从 `openapi.json` 生成的契约类型 | **生成的，不要手改** |
| `Sources/ERCore` | 内核：缓存、发件箱、复习状态机、显示语义 | 手写 |
| `Sources/ercli` | 命令行客户端，P5 的验收工具 | 手写 |
| `Tests/ERCoreTests/Fixtures` | 真实响应导出的样本、复习状态机的测试向量 | **后端导出的，不要手改** |

## 两条硬规矩

**`ERCore` 里不许出现任何界面框架**（`UIKit` / `SwiftUI` / `AppKit`）。
判据是：换成安卓客户端也要一模一样的归 Core，会长得不一样的归界面。
CI 里有一行 grep 守着这条。

**`ERCore` 不实现网络，只定义协议。**真正发请求的实现由宿主提供——
P5 是命令行客户端，P6 是 iOS 端。这样 Core 在 Windows / Linux / iOS 上行为一致，
测试也不需要任何网络桩。

## 本地怎么跑

Swift 6.3.3 装在 `D:\Software\Swift`。**新开的终端已经带好环境**；
只有在安装之前就启动的 shell 才要手动补 PATH 和 `SDKROOT`（见根目录 CLAUDE.md）。

```
cd client
swift build
swift test
```

**代理有两条要守**，否则会很难查：只设大写的 `NO_PROXY`（两种大小写都设会让
Foundation 建环境字典时撞重复键，进程直接崩），而且它必须设上（不设的话发给
本机后端的请求会被代理接管，「连不上」变成「503」）。

## 生成的东西怎么来的

```
python scripts/export_contract.py      # 导出 openapi.json 与真实响应样本
python scripts/export_review_vectors.py  # 导出复习状态机的测试向量
```

两者的产物都**提交进仓库**，CI 检查「重新导出之后没有 diff」——
契约一变，检查当场变红。详见 `docs/phase-5.html` §8、§9。
