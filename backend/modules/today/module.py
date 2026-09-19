"""今日包模块：把当天的阅读与复习合成一份。

Its own module rather than a route added to reading, because it is a composer:
it depends on both reading and review, and neither should depend on it. Putting
it inside reading would have made reading import review, which is backwards —
review already builds on reading's articles and sentences.

Nothing else changed to add it. That is the property Phase 0 established and
this is one more instance of it: a directory with a ``MODULE`` in it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from backend.core import auth, runtime_config
from backend.core.registry import Module
from backend.modules.today import service
from backend.core.contract import Capabilities, Learner
from backend.modules.reading import service as reading
from backend.modules.today.contract import TodayResponse
from pydantic import BaseModel

client_router = APIRouter()

DeviceId = Annotated[int, Depends(auth.require_device)]


@client_router.get("/today", summary="今日包：当天的文章与复习，一次拿全",
                   response_model=TodayResponse)
async def today(device_id: DeviceId, request: Request, response: Response) -> Any:
    """One response the client can work from all day without a network.

    New endpoint rather than fields added to an existing one — 架构铁律 5 lets
    us add, never change, and P2 deliberately left this shape unguessed
    (`phase-2.html` §6) rather than bolting an invented one onto `/articles`.

    **Does not include exam papers** (决定 9). They have their own entrance at
    ``/v1/client/library?source=cet4|cet6|kaoyan``. The response says so in
    ``excludes_exam_papers`` so a client cannot conclude otherwise by accident.

    **条件请求（2026-09-16 加）。** 这个包实测约 1 MB，而客户端每次开复习那一格
    都要它——绝大多数时候内容跟手机上那份**一模一样**，却照样过一遍隧道
    （实测 0.85–1.4 秒，流量也是真金白银）。带 ``If-None-Match`` 来、内容没变，
    就回 304 和零字节。

    **加的是一个响应头和一条分支，不是新字段**，所以老客户端一行不用改：
    它不发 ``If-None-Match``，就永远走 200 那条路（铁律 5）。

    哈希算的是**序列化之后的响应体**，不是它的某几个字段——任何一处变化都算变化，
    包括复习进度、文章读到哪。宁可多发一次，也不能把变了的说成没变。
    """
    payload = service.package(auth.learner_for_device(device_id))
    etag = _etag_for(payload)

    if request.headers.get("if-none-match") == etag:
        # 304 不带包体，也不该带 `Content-Type`——它说的是「你那份还作数」。
        return Response(status_code=304, headers={"ETag": etag})

    response.headers["ETag"] = etag
    return payload


class MeResponse(BaseModel):
    '''这把令牌是谁的，以及哪些留好的位置真的有值了。

    **测试连接用的就是它。** 在这之前那个探针打的是日历那个端点
    （「最轻的一个」），而日历 P9 搬到了设备上——探针因此需要一个真正最轻的:
    不读学习记录、不组装任何东西、不下发一个字节的内容。

    **它同时回答了探针真正关心的两件事**:令牌认不认（401 还是 200），
    以及对面是谁（换过令牌之后名字对不对）。
    '''

    learner: Learner
    capabilities: Capabilities


@client_router.get("/me", summary="这把令牌是谁的（测试连接用）",
                   response_model=MeResponse)
async def me(device_id: DeviceId) -> Any:
    learner_id = auth.learner_for_device(device_id)
    return {
        "learner": auth.learner_profile(learner_id),
        "capabilities": reading.capabilities(),
    }


def _etag_for(payload: dict[str, Any]) -> str:
    """This package's version, for conditional requests.

    **两个字段要排除在外：``clock.simulated_now`` 与 ``clock.real_now``。**
    它们是「这个响应是什么时候生成的」，不是「里面装了什么」——而它们精确到秒，
    所以把整包原样哈希的话，**指纹每秒都变，条件请求永远命中不了**。
    2026-09-16 实测到：两次相邻请求，只有这两个字段不同。

    客户端要的是时钟的 ``offset_days`` 和 ``simulated``（日历按模拟时钟走），
    那两个仍然照常下发，只是不参与指纹。

    **除此之外一处不漏**：复习进度、文章读到哪，任何变化都要算变化。
    宁可多发一次，也不能把变了的说成没变。
    """
    clock = (payload.get("reviews") or {}).get("clock")
    stashed: dict[str, Any] = {}
    if isinstance(clock, dict):
        for field in ("simulated_now", "real_now"):
            if field in clock:
                stashed[field] = clock.pop(field)
    try:
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    finally:
        if isinstance(clock, dict):
            clock.update(stashed)
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'


runtime_config.register(
    runtime_config.ConfigSpec(
        key="today_article_count",
        default=3,
        value_type="int",
        title="今日包装几篇正课",
        description="每天备稿的篇数一般也是这个数。没读完的不会消失，会留在往期。",
        group="today",
        order=10,
    ),
    runtime_config.ConfigSpec(
        key="today_extra_count",
        default=0,
        value_type="int",
        title="今日包装几篇加餐",
        description="2026-09-12 改为 0：加餐取消了。往期本身就是加餐——备好没读的一直堆着"
        "（实测净增两篇一天），往列表下面翻就有，再压一篇没有意义。"
        "字段 extra_articles 留在响应里不删（铁律 5 只增不减），改回非零就能恢复。"
        "原文与理由见归档 §I。",
        group="today",
        order=11,
    ),
)

MODULE = Module(
    name="today",
    title="今日包",
    description="把当天的文章与复习合成一份，客户端拿了可以整天离线用",
    client_router=client_router,
)
