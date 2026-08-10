"""同步模块启动引导：幂等创建内置 TaoSync 引擎。

内置引擎 ``engineType='taosync'`` / ``systemKey='taosync'`` / ``protected=1``，
对应 TaoSync 源项目里受保护的内置引擎，仅它之下可管理 local 存储目录。
"""
from __future__ import annotations

import time

from db import get_session_local

from models_sync import SyncEngine


def ensure_builtin_engine(session_factory=None):
    """若不存在内置 taosync 引擎，则创建一个受保护的实例。返回其 id。"""
    sf = session_factory or get_session_local()
    with sf() as db:
        existing = (
            db.query(SyncEngine)
            .filter_by(engineType="taosync", systemKey="taosync")
            .first()
        )
        if existing is not None:
            return existing.id
        obj = SyncEngine(
            remark="TaoSync（内置）",
            url="",
            userName="TaoSync",
            token="",
            engineType="taosync",
            systemKey="taosync",
            protected=1,
            createTime=int(time.time()),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
