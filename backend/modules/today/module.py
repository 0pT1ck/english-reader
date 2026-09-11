"""今日包模块：把当天的阅读与复习合成一份。

Its own module rather than a route added to reading, because it is a composer:
it depends on both reading and review, and neither should depend on it. Putting
it inside reading would have made reading import review, which is backwards —
review already builds on reading's articles and sentences.

Nothing else changed to add it. That is the property Phase 0 established and
this is one more instance of it: a directory with a ``MODULE`` in it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from backend.core import auth, runtime_config
from backend.core.registry import Module
from backend.modules.today import service

client_router = APIRouter()

DeviceId = Annotated[int, Depends(auth.require_device)]


@client_router.get("/today", summary="今日包：当天的文章与复习，一次拿全")
async def today(device_id: DeviceId) -> dict[str, Any]:
    """One response the client can work from all day without a network.

    New endpoint rather than fields added to an existing one — 架构铁律 5 lets
    us add, never change, and P2 deliberately left this shape unguessed
    (`phase-2.html` §6) rather than bolting an invented one onto `/articles`.

    **Does not include exam papers** (决定 9). They have their own entrance at
    ``/v1/client/library?source=cet4|cet6|kaoyan``. The response says so in
    ``excludes_exam_papers`` so a client cannot conclude otherwise by accident.
    """
    return service.package(auth.learner_for_device(device_id))


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
        default=1,
        value_type="int",
        title="今日包装几篇加餐",
        description="读完主线之后解锁。还想读就再读一篇——原本「装次日到期的复习词」"
        "那个性质随 E2 一起移出了（归档 §G）。",
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
