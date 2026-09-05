# 怎么加一个功能模块

一个模块 = 一个自包含的功能。它自带数据表、接口、管理页面、事件订阅、配置项和后台任务。

**加一个模块不需要修改这个目录以外的任何文件。** 没有路由表要登记，没有导航菜单要添加，没有迁移索引要维护。模块由目录扫描自动发现——那种"别忘了去某处登记一下"的步骤迟早会被忘记，也迟早会变成冲突点。

这条性质是 Phase 0 的第 6 项验收内容。如果哪天加功能开始需要改动别处，说明骨架退化了，应该先修骨架而不是绕过去。

## 最快的做法

复制 `example/` 整个目录，改名，替换内容。它把所有扩展点都用了一遍，是可运行的样板。

## 目录结构

```
backend/modules/<name>/
├── __init__.py       模块说明
├── module.py         必需：声明 MODULE 对象，注册表只找这个
├── schema.py         迁移（内容多时单独放，少则直接写在 module.py）
├── routes.py         接口与页面
├── service.py        业务逻辑
└── templates/        本模块的页面模板
```

只有 `module.py` 和其中的 `MODULE` 是硬性要求，其余按需要拆分。

## 六个扩展点

### 1. 数据表

```python
MIGRATIONS = [
    Migration(version=1, name="...", database="learning", apply="CREATE TABLE ..."),
]
```

`version` 只需在**本模块内**递增，不用和别的模块协调编号。`database` 三选一：

- `learning` — 学习数据，不可再生，**升级前会自动备份**
- `dictionary` — 词典参考数据，可随时重建
- `logs` — 技术日志，可丢弃

改表结构永远通过新增一个迁移，不要修改已经应用过的那个。

### 2. 事件订阅

```python
subscriptions={"reading.finished": [on_reading_finished]}
```

这是接入新功能的首选方式。绝大多数功能本质上是"在某件事发生时顺便做点什么"——用订阅接入，发出事件的那段代码一行都不用动。

处理函数是同步的。抛异常不会影响其他订阅者，也不会影响发出事件的那个操作，但会记为 ERROR（并触发告警和 DEBUG 落盘）。

发出事件：`events.emit("thing.happened", key=value)`。事件名用点分隔的稳定标识符，和日志事件名同一套规范。

### 3. 接口

```python
client_router=...   # 挂到 /v1/client，供阅读客户端使用
admin_router=...    # 挂到 /v1/admin，供管理控制台使用
```

两者的认证完全分开，客户端令牌碰不到管理接口。给路由加依赖：客户端用 `Depends(auth.require_device)`，管理用 `Depends(auth.require_admin)`。

接口一旦发布就要向后兼容：可以加字段，不能删字段或改字段含义。

### 4. 管理页面

```python
admin_router_pages=...   # 用绝对路径，如 /admin/<name>
admin_pages=[AdminPage(title="...", path="/admin/<name>", order=50)]
add_template_dir(Path(__file__).parent / "templates")
```

页面模板 `{% extends "base.html" %}`，导航自动出现。页面取数一律走自己的管理接口，不要直接查库——这样界面和接口不会脱节，排查问题时也能绕过界面直接调接口。

### 5. 配置项

```python
runtime_config.register(runtime_config.ConfigSpec(
    key="...", default=..., value_type="int",
    title="中文标题", description="中文说明", group="...",
))
```

谁拥有这个设置，就在谁那里声明。不要维护一份"全系统配置总表"——那会变成每个阶段都要改的文件。

参数一律配置化，不要硬编码。

### 6. 启动钩子

```python
on_startup=warm_something
```

在迁移之后、服务开始接受请求之前执行一次。用于预热之类的工作。失败会被记录但不会阻止服务启动。

## 注意事项

- **注释用英文，解释「为什么」而不是复述代码。** 使用者不读代码，注释是写给以后接手的 AI 看的。项目特有概念在模块头部给中英对照。
- **界面文字用中文。**
- **日志事件名英文、说明文字中文。** 参见 `core/logging.py`。
- **不要跨阶段实现功能。** 当前阶段的边界写在对应的 `docs/phase-N.html` 里。数据结构可以为将来预留字段，但不实现将来的逻辑。
- 模块目录以 `_` 开头会被跳过，可用于暂时禁用。
