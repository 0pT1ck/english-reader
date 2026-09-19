"""Review — P3.

复习 review / 看词想义 word→sense / 看义想词 sense→word / 句子池 sentence pool

What P2 left undone: you marked a word and nothing ever happened to it. This
module is the other half of the promise — a marked sense comes back on its own,
at a time the memory state decides.

Everything is in this directory except one thing: the memory columns on
``study_states``, added by ``reading``'s migration 5 because reading owns that
table. That is the same rule that put ``devices.learner_id`` in core auth.

The plan, decision by decision, is ``docs/phase-3.html`` §2.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core import runtime_config
from backend.core.logging import get_logger
from backend.core.events import Event
from backend.core.registry import AdminPage, Module
from backend.modules.llm import jobs
from backend.modules.review import repository, routes, sentences, translate
from backend.modules.review.schema import MIGRATIONS

log = get_logger("review")

add_template_dir(Path(__file__).parent / "templates")


runtime_config.register(
    runtime_config.ConfigSpec(
        key="review_pool_target",
        default=8,
        value_type="int",
        title="每个义项的句子池目标条数",
        description=(
            "复习出题用的句子，每个义项备多少条。只见过一句的话，记住的很可能是那句话，"
            "换一句就又不认识了——所以要有一池子随机抽。"
            "语料库能供的中位数只有 3 条，差额由模型生成补足。"
        ),
        group="review",
        order=10,
    ),
    runtime_config.ConfigSpec(
        key="review_generate_batch",
        default=12,
        value_type="int",
        title="生成句子时一次要几条",
        description=(
            "过量生成再逐句机器筛，留够目标条数就行。"
            "本项目已三次证明模型对「写 N 条好的」这类数量约束无感，所以多要几条比反复叮嘱有效。"
        ),
        group="review",
        order=20,
    ),
    runtime_config.ConfigSpec(
        key="review_weight_decay",
        default=0.5,
        value_type="float",
        title="答错之后被抽中概率的衰减系数",
        description=(
            "当天答错一次，这个词被抽中的权重就乘以这个数。老不会的词自己让路，不阻碍别的词，"
            "但从不被排除——当天结束的唯一条件是池子空了。"
        ),
        group="review",
        order=30,
    ),
    runtime_config.ConfigSpec(
        key="review_spelling",
        default=True,
        value_type="bool",
        title="当天复习完之后提供拼写强化",
        description=(
            "可选的加强项，针对当天复习过的所有词、按词去重。"
            "拼错不影响复习时间的安排，只单独记一笔（记下拼错成了什么），留给将来主攻拼写的功能。"
        ),
        group="review",
        order=40,
    ),
    runtime_config.ConfigSpec(
        key="review_gen_provider",
        default="dsflash",
        value_type="str",
        title="生成例句用哪个提供商",
        description=(
            "2026-09-09 横评 23 个模型的结论：造例句的合格率 92–100%，不需要用贵模型，"
            "deepseek-v4-flash 96% 反而高于 gpt-5.5 94%。"
            "注意别跟「写整篇文章要用 gpt-5.5」那条搞混——任务大小不同。"
        ),
        group="review",
        order=50,
    ),
    runtime_config.ConfigSpec(
        key="review_clock_offset_days",
        default=0,
        value_type="int",
        title="模拟时钟前进了几天（开发用）",
        description=(
            "复习是按天调度的，不快进就得真等几天才看得到第二次复习长什么样。"
            "这个数会加在复习模块看到的「现在」上——到期判断、会话属于哪一天、"
            "交给调度器的时间、写进历史的时间戳，全都一起挪。"
            "**它在骗系统说今天是哪天，所以别忘了归零**：复习页在它非零时会一直挂着提示，"
            "验收脚本在时钟被挪动时直接判失败。"
        ),
        group="review",
        order=95,
    ),
    runtime_config.ConfigSpec(
        key="review_web_token",
        default="",
        value_type="str",
        title="Web 复习页的设备令牌",
        description=(
            "开发期复习页用它走 /v1/client，跟别的客户端同一条路——"
            "不这样它就等于没在验客户端契约。首次打开自动签发。"
        ),
        group="review",
        order=90,
        secret=True,
    ),
    runtime_config.ConfigSpec(
        key="fsrs_parameters",
        # **写死这 21 个数，不再「留空 ＝ 用库的默认」**（P9 §11）。
        #
        # 排期搬到了客户端，所以服务端不再有 FSRS 的实现可以去问「你的默认是什么」。
        # 而「留空」这个约定本来就危险:两个包自带的默认**不是同一组数**
        # （py-fsrs 6.3.2 是 FSRS-6 的 21 个，那个官方 Swift 包默认仍是 FSRS-5 的 19 个），
        # 两边各取自己的默认就会跑出不同的间隔——**而两边都在按自己的文档正常工作、
        # 没有东西会报错**。参数本来就是配置，现在它就长得像配置。
        #
        # 这组数是 py-fsrs 6.3.2 的默认值，2026-09-18 照抄进来；
        # 它同时记在 `scheduler-vectors.json` 的 `settings.parameters` 里，
        # 而那份向量是拿它导出来的——两处对不上，向量那一套会红。
        default=[
            0.212, 1.2931, 2.3065, 8.2956, 6.4133,
            0.8334, 3.0194, 0.001, 1.8722, 0.1666,
            0.796, 1.4835, 0.0614, 0.2629, 1.6483,
            0.6014, 1.8729, 0.5425, 0.0912, 0.0658,
            0.1542,
        ],
        value_type="json",
        title="FSRS 参数（21 个数）",
        description=(
            "21 个数，FSRS-6。默认这一组是从数百万条真实复习记录拟合出来的。"
            "将来攒够自己的复习历史，可以用 optimizer 重新拟合成你个人的遗忘曲线，再填到这里。"
            "**客户端按下发的这一组算排期**，所以改了它，手机上的间隔跟着变。"
        ),
        group="review",
        order=60,
    ),
    runtime_config.ConfigSpec(
        key="fsrs_desired_retention",
        default=0.9,
        value_type="float",
        title="目标记忆保持率",
        description=(
            "调度会把间隔安排成「到期时你大约还记得住这个比例」。"
            "调高就复习得更勤、记得更牢但量更大；调低反之。0.9 是通行的起点。"
        ),
        group="review",
        order=70,
    ),
    runtime_config.ConfigSpec(
        key="fsrs_maximum_interval",
        default=180,
        value_type="int",
        title="最长复习间隔（天）",
        description=(
            "FSRS 自带的默认值是 36500 天——那是给「终身记住」设计的。"
            "本项目的目标是有日期的考试：排到考试之后再复习等于没复习，"
            "而且「稳固掌握需要遇见 8–12 次」那条也就永远达不到。"
            "实测不压的话第三次复习就排到 397 天以后。180 天是半年，够长也够得着。"
        ),
        group="review",
        order=75,
    ),
    runtime_config.ConfigSpec(
        key="fsrs_fuzz",
        default=True,
        value_type="bool",
        title="给到期时间加随机抖动",
        description=(
            "同一天标记的一批词，不加抖动就会永远在同一天一起回来。"
            "抖动几个百分点能把这种撞车摊平一些——它不解决主文档 §M 记着的负载均衡问题，但便宜且有用。"
            "验收脚本会关掉它，否则结果不可复现。"
        ),
        group="review",
        order=80,
    ),
    runtime_config.ConfigSpec(
        key="review_translate_batch",
        default=10,
        value_type="int",
        title="翻译句子时一批几条",
        description=(
            "看中文想英文那个方向的题面，是句子的中文译文。翻译一条一条互不相干，"
            "但输出长度会累积——P1c 实测过快模型在长结构化列表的后半段会漂。"
            "所以批次切小，宁可多跑几批。"
        ),
        group="review",
        order=90,
    ),
    runtime_config.ConfigSpec(
        key="review_translate_provider",
        default="",
        value_type="str",
        title="翻译句子用哪个提供商",
        description=(
            "留空就走默认提供商。翻译是整理不是写作，要的是快模型。"
            "2026-09-13 实测：中转站上 deepseek-flash 与 deepseek-v4-flash-0731 都可用，"
            "而且是两个不同的模型、不是别名。"
        ),
        group="review",
        order=91,
    ),
)


def _register_workers() -> None:
    """Batch work this module contributes. Registered at startup so the module
    can be removed as a directory without leaving a dangling job kind."""
    jobs.register_worker(sentences.WORKER)
    jobs.register_worker(translate.WORKER)


# `on_word_unmarked` 没有了（P9 §11）。它做的是「撤回标记之后把今天那道题收掉」，
# 而今天那道题现在根本不在服务端——队列由设备重放自己的日志算出来，撤回标记就是
# 日志里的一条 `word.unmarked`，下一次重放它自己就不在了。
#
# **这一条值得留个记号**:它是 2026-09-16 真机上逼出来的修复（点「我已经会了」，
# 那个词照样来），当时补的是「意图写了、实现只做了一半」那个坑（§7.1）。
# 现在它消失不是因为问题没了，是因为那半边状态消失了——同一个 bug 在新架构里
# 造不出来。**这是这条线画对了的一个证据，不是一次退步。**


def on_article_finished(event: Event) -> None:
    """Top up the sentence pools the moment an article is read.

    决定 23: sentences are generated when you finish reading, not overnight —
    the first review of a word you marked today happens **today**, and it needs
    a sentence you have not seen. Waiting for a nightly batch would leave that
    first round with nothing to ask.

    Attached by subscription, so nothing in the reading pipeline knows this
    module exists (architecture rule 6). Reading emits that an article was
    finished; what that means for review is review's business.

    Silent when every pool is already full — ``start_for`` returns ``None`` and
    no job is created.
    """
    learner_id = int(event.get("learner_id") or 1)
    try:
        job_id = sentences.start_for(learner_id=learner_id)
    except Exception as exc:  # noqa: BLE001 - never let this break finishing an article
        log.warning("review.autogen.failed",
                    f"读完文章后自动补句子没能启动：{exc}",
                    article_id=event.get("article_id"))
        return
    if job_id:
        log.info("review.autogen.started",
                 f"读完文章 {event.get('article_id')} 后开始补句子池",
                 article_id=event.get("article_id"), job_id=job_id)


def on_pool_reported(event: Event) -> None:
    """Top up the sentence pools when the device says what it is learning.

    **This is the signal, and `article.finished` is only the older half of it.**
    Which senses need sentences is a function of which senses are being learned,
    and as of P9 that answer arrives from the device (`phase-9.html` §7) rather
    than being derived here. A word marked while reading is not in the snapshot
    the server holds until the device reports again — and the device reports
    *after* it pushes its events, so the finish event alone would miss exactly
    the words marked today. Silently: nothing errors, the word simply has no
    question when it first comes up.

    Silent when every pool is already full — ``start_for`` returns ``None``.
    """
    learner_id = int(event.get("learner_id") or 1)
    if not int(event.get("reviewing") or 0):
        return
    try:
        job_id = sentences.start_for(learner_id=learner_id)
    except Exception as exc:  # noqa: BLE001 - reporting a snapshot must not fail on this
        log.warning("review.autogen.failed",
                    f"收到词池快照后自动补句子没能启动：{exc}")
        return
    if job_id:
        log.info("review.autogen.started",
                 f"收到词池快照后开始补句子池（在学 {event.get('reviewing')} 条）",
                 job_id=job_id, reviewing=event.get("reviewing"))


MODULE = Module(
    name="review",
    title="复习",
    description="标记过的词按记忆状态自己回来：双向、渐进提示、句子池、拼写强化",
    migrations=MIGRATIONS,
    client_router=routes.client_router,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        # 「复习」那一页 2026-09-18 删了（P9 §11）:它要能复习就得在 JS 里
        # 再实现一遍学习引擎，而那是那条线禁止的事。复习在手机和 `ercli` 上。
        AdminPage(
            title="词表",
            path="/admin/review/words",
            order=36,
            description="标记过的每个词：标记、出处、句子池，以及设备报的词池",
        ),
    ],
    on_startup=_register_workers,
    subscriptions={"article.finished": [on_article_finished],
                   "progress.pool.reported": [on_pool_reported]},
)
