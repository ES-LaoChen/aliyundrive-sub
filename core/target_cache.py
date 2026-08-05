"""目标目录预检缓存（T-D1，PRD T08）。

设计：
- 内存 dict 缓存，5 分钟 TTL；
- key = folder_id，value = (exists: bool, ts: datetime)；
- ``exists_or_probe(folder_id, probe_fn)`` 命中即返回，未命中调用 probe_fn 探测并写入；
- 订阅被删除时上层调用 ``invalidate(folder_id)`` 清空条目（预留接口，本期订阅删除逻辑可选择性接入）。

不在本期落 DB 持久化：进程重启即失效，与「短期缓存」语义一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


class TargetCache:
    """目标目录存在性缓存（5 分钟 TTL）。"""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._cache: dict[str, tuple[bool, datetime]] = {}
        # 探测失败的次数：用于在 probe 阶段就避免狂打 list_files
        # （本期不实现自适应退避，仅作预留统计）。
        self._miss_streak: dict[str, int] = {}

    def exists_or_probe(
        self,
        folder_id: str,
        probe_fn: Callable[[str], bool],
    ) -> bool:
        """返回 ``folder_id`` 是否存在。

        命中缓存（未过期）→ 直接返回 ``cached_value``；
        未命中/已过期 → 调 ``probe_fn(folder_id)`` 探测并写入缓存。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cached = self._cache.get(folder_id)
        if cached is not None:
            exists, ts = cached
            if now - ts <= self._ttl:
                return exists
        # 未命中 / 过期：探测
        try:
            exists = bool(probe_fn(folder_id))
        except Exception:
            # 探测失败按「不存在」处理，避免因网络问题误判为有效。
            exists = False
            self._miss_streak[folder_id] = self._miss_streak.get(folder_id, 0) + 1
        self._cache[folder_id] = (exists, now)
        return exists

    def invalidate(self, folder_id: str) -> None:
        """清除指定 folder_id 的缓存条目（订阅删除/目标目录变更时调用）。"""
        self._cache.pop(folder_id, None)
        self._miss_streak.pop(folder_id, None)

    def clear(self) -> None:
        """清空所有缓存（测试用）。"""
        self._cache.clear()
        self._miss_streak.clear()

    def stats(self) -> dict[str, int]:
        """返回缓存统计快照（调试 / 测试用）。"""
        return {
            "size": len(self._cache),
            "miss_streak_total": sum(self._miss_streak.values()),
        }

    def peek(self, folder_id: str) -> Optional[tuple[bool, datetime]]:
        """仅查看缓存（不探测），测试用。"""
        return self._cache.get(folder_id)
