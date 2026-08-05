"""订阅级并发锁管理器（T-D1 / T-D4，PRD T05）。

设计（单 worker 足够）：
- 内部 ``dict[int, asyncio.Lock]``；
- 顶层 ``asyncio.Lock`` 保护 dict 自身（避免并发首次访问时创建/获取竞态）；
- ``try_acquire(sub_id)`` 立即返回：成功 → 返回 ``(lock, True)``；失败 → 返回 ``(None, False)``；
- 调用方负责 ``release(sub_id)``；
- 订阅删除时 ``cleanup(sub_id)`` 移除 dict 条目（避免长跑后内存泄漏）。

注意：``asyncio.Lock`` 必须在运行的事件循环内创建/获取；本类采用「按需创建」策略
（首次 ``try_acquire`` 时在调用方的事件循环里创建 Lock）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SubLockManager:
    """订阅级并发锁管理器。"""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        # guard 保护 _locks 自身；首次创建时 lazy init。
        self._guard: Optional[asyncio.Lock] = None
        # 持锁计数（仅用于统计/日志，便于排查泄漏）
        self._held_count: dict[int, int] = {}

    def _get_guard(self) -> asyncio.Lock:
        if self._guard is None:
            self._guard = asyncio.Lock()
        return self._guard

    def _get_lock(self, sub_id: int) -> asyncio.Lock:
        if sub_id not in self._locks:
            self._locks[sub_id] = asyncio.Lock()
            self._held_count[sub_id] = 0
        return self._locks[sub_id]

    async def try_acquire(self, sub_id: int) -> tuple[Optional[asyncio.Lock], bool]:
        """尝试立即获取订阅锁。

        Returns:
            ``(lock, True)`` 表示获取成功，调用方负责 ``release(sub_id)``；
            ``(None, False)`` 表示锁已被其他协程持有。
        """
        guard = self._get_guard()
        async with guard:
            lock = self._get_lock(sub_id)
        # 非阻塞尝试获取
        if lock.locked():
            return None, False
        # lock.locked() 返回 False 但仍可能竞争态：这里 acquire() 不会挂
        acquired = await asyncio.wait_for(lock.acquire(), timeout=0.001)
        if not acquired:
            return None, False
        async with guard:
            self._held_count[sub_id] = self._held_count.get(sub_id, 0) + 1
        return lock, True

    def release(self, sub_id: int) -> None:
        """释放订阅锁。"""
        lock = self._locks.get(sub_id)
        if lock is None:
            logger.warning("release: 订阅 %s 无锁记录", sub_id)
            return
        if not lock.locked():
            logger.warning("release: 订阅 %s 锁未被持有", sub_id)
            return
        lock.release()
        # 计数修正（无需 guard，hold 计数仅供统计）。
        self._held_count[sub_id] = max(0, self._held_count.get(sub_id, 1) - 1)

    def is_locked(self, sub_id: int) -> bool:
        """仅查询锁状态（不获取），测试 / 日志用。"""
        lock = self._locks.get(sub_id)
        return bool(lock and lock.locked())

    def cleanup(self, sub_id: int) -> None:
        """订阅删除时清理条目（仅当锁未被持有时生效；否则下次 release 后会留下空 key）。"""
        # 不强行删 _locks[sub_id]，否则持锁中调用 release 会丢锁。
        # 改：仅移除 _held_count 残留，_locks 在 release 时按需清理。
        self._held_count.pop(sub_id, None)

    def held_count(self, sub_id: int) -> int:
        return self._held_count.get(sub_id, 0)
