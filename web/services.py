"""Web 层共享的服务容器。

把进程级单例（client / checker / scheduler / notifier / aria2 / settings 等）
打包后注入 Flask ``app.config["SERVICES"]``，蓝图按需取用。

T-D1 增补三个字段（向前兼容，使用 ``default=None``）：
- ``transfer_repo``: 任务 / 运行仓储
- ``target_cache``: 目标目录预检缓存
- ``sub_lock``: 订阅级并发锁管理器
- ``retry_policy``: 转存重试策略（默认配置；测试可覆盖）

调用方约定：使用 ``getattr(svc, 'transfer_repo', None)`` 兜底，旧的测试
构造 ``Services(...)`` 不传新字段时不会崩溃。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Services:
    """聚合所有运行时服务，便于蓝图统一访问。"""

    settings: Any
    session_factory: Any
    client: Any
    checker: Any
    scheduler: Any
    notifier: Any
    aria2: Any
    naming: Any
    token_store: Any
    # ----- T-D1 新增字段（向前兼容：旧调用方可不传） -----
    transfer_repo: Any = None
    target_cache: Any = None
    sub_lock: Any = None
    retry_policy: Any = None
    # ----- T-TG：Telegram 频道监控自动转存服务（向前兼容：默认 None） -----
    tg_monitor: Any = None
    # ----- 同步管理（移植自 TaoSync）：作业编排 + 多后端存储目录管理 -----
    sync_service: Any = None
