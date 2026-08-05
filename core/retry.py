"""退避重试装饰器（T-D2，PRD T02）。

设计：
- ``RetryPolicy`` 数据类：max_attempts / base / cap / retriable_kinds
- ``with_retry(policy, fn, *args, **kwargs)`` 装饰器：
  - 仅对 ``policy.retriable_kinds`` 内的 ``TransferErrorKind`` 走 sleep + retry
  - 不可重试 / 已用尽 → 抛 ``TransferError(kind, attempts=..., original=exc)``
  - 指数退避 ``min(base * 2**attempt, cap)`` + ±20% 抖动
- 同步函数（避免引入 asyncio 复杂度；外层 ``_transfer_one`` 仍是同步调用 client）。
- 关键日志通过 ``log_event`` helper 发出（``EVT_TRANSFER_RETRY`` / ``EVT_TRANSFER_FAIL``）。

不在本期引入 ``tenacity`` 等三方依赖。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, TypeVar

from core.error_classifier import ErrorClassifier
from core.log_fields import EVT_TRANSFER_FAIL, EVT_TRANSFER_RETRY, log_event
from core.types import TransferError, TransferErrorKind

logger = logging.getLogger(__name__)

# 永远不可重试的错误类型（即便用户配置 retriable_kinds 也不生效）
_NON_RETRIABLE_KINDS = frozenset({
    TransferErrorKind.SHARE_EXPIRED,
    TransferErrorKind.PROOF_INVALID,
    TransferErrorKind.QUOTA_FULL,
    TransferErrorKind.TARGET_MISSING,
    TransferErrorKind.FILE_NOT_FOUND,
    TransferErrorKind.INVALID_PARAMETER,
})

T = TypeVar("T")


@dataclass
class RetryPolicy:
    """退避重试策略。"""

    max_attempts: int = 3
    base: float = 1.0
    cap: float = 8.0
    retriable_kinds: set[str] = field(
        default_factory=lambda: {TransferErrorKind.NETWORK.value, TransferErrorKind.RATE_LIMITED.value}
    )
    # 抖动比例（默认 ±20%）
    jitter: float = 0.2

    def is_retriable(self, kind: TransferErrorKind) -> bool:
        """判断该错误类型是否可重试。

        业务硬约束（DESIGN §附录 A、PRD Q6）：以下类型**永远**不可重试，
        即便用户把它们加进 ``retriable_kinds`` 也不生效：
          - ``SHARE_EXPIRED``  分享失效（人工替换）
          - ``PROOF_INVALID``  proof 校验失败（避免浪费配额）
          - ``QUOTA_FULL``     空间已满（重试无意义）
          - ``TARGET_MISSING`` 目标目录缺失
          - ``FILE_NOT_FOUND`` 源文件已不在分享
          - ``INVALID_PARAMETER`` 参数错（重试无意义）
        """
        if kind in _NON_RETRIABLE_KINDS:
            return False
        return kind.value in self.retriable_kinds

    def compute_delay(self, attempt: int) -> float:
        """``compute_delay(attempt) = min(base * 2**attempt, cap)`` + 抖动。

        Args:
            attempt: 已重试次数（0 表示第一次重试前等待 1*base）。
        """
        raw = self.base * (2 ** attempt)
        capped = min(raw, self.cap)
        # 抖动：capped * (1 ± jitter)
        if self.jitter > 0:
            factor = 1.0 + random.uniform(-self.jitter, self.jitter)
            capped = max(0.0, capped * factor)
        return capped


def classify_to_transfer_error(
    exc: BaseException,
    attempts: int = 0,
) -> TransferError:
    """把任意异常包成 ``TransferError``。"""
    kind = ErrorClassifier.classify(exc)
    code = getattr(exc, "code", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    return TransferError(
        kind=kind, message=message, code=code, attempts=attempts, original=exc
    )


def with_retry(
    policy: RetryPolicy,
    fn: Callable[..., T],
    *args: Any,
    on_event: Optional[Callable[[dict], None]] = None,
    **kwargs: Any,
) -> T:
    """带退避重试的同步函数装饰器。

    流程：
        attempt=0 → 调 fn() → 成功则 return
                  → 失败则 classify → 不可重试 / 已用尽：raise TransferError
                                    → 可重试：sleep compute_delay(attempt) → attempt += 1 → 继续

    Args:
        policy: 重试策略。
        fn: 目标函数（同步）。
        *args / **kwargs: 透传给 fn。
        on_event: 回调，传入 ``{"event": "retry"/"fail", "attempt": N, "kind": str, "elapsed_ms": int}``。
                  主要给测试断言用，生产代码不传。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(policy.max_attempts):
        t0 = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # 最终成功：第一次成功不记录 retry 事件，避免噪音
            if attempt > 0:
                log_event(
                    EVT_TRANSFER_RETRY,
                    level=logging.INFO,
                    attempts=attempt,
                    elapsed_ms=elapsed_ms,
                    extra={"event_outcome": "ok", "final_attempt": attempt + 1},
                )
                if on_event is not None:
                    on_event({"event": "ok", "attempt": attempt, "elapsed_ms": elapsed_ms})
            return result
        except Exception as exc:  # noqa: BLE001  # 装饰器需捕获所有业务异常
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            kind = ErrorClassifier.classify(exc)
            last_exc = exc
            is_last = attempt + 1 >= policy.max_attempts
            if not policy.is_retriable(kind) or is_last:
                # 不可重试 或 已用尽
                level = logging.ERROR if is_last or not policy.is_retriable(kind) else logging.WARNING
                evt = EVT_TRANSFER_FAIL if is_last or not policy.is_retriable(kind) else EVT_TRANSFER_RETRY
                log_event(
                    evt,
                    level=level,
                    attempts=attempt + 1,
                    error_kind=kind.value,
                    elapsed_ms=elapsed_ms,
                    extra={"event_outcome": "fail", "message": str(exc)[:200]},
                )
                if on_event is not None:
                    on_event({
                        "event": "fail",
                        "attempt": attempt + 1,
                        "kind": kind.value,
                        "elapsed_ms": elapsed_ms,
                    })
                raise classify_to_transfer_error(exc, attempts=attempt + 1) from exc
            # 可重试：sleep + 下一轮
            delay = policy.compute_delay(attempt)
            log_event(
                EVT_TRANSFER_RETRY,
                level=logging.WARNING,
                attempts=attempt + 1,
                error_kind=kind.value,
                elapsed_ms=elapsed_ms,
                extra={"event_outcome": "retry", "sleep_seconds": round(delay, 3)},
            )
            if on_event is not None:
                on_event({
                    "event": "retry",
                    "attempt": attempt + 1,
                    "kind": kind.value,
                    "elapsed_ms": elapsed_ms,
                    "delay": delay,
                })
            time.sleep(delay)
    # 理论上不可达；为了类型安全
    assert last_exc is not None
    raise classify_to_transfer_error(last_exc, attempts=policy.max_attempts) from last_exc
