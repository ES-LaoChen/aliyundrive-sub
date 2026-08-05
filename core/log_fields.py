"""结构化日志事件常量与 helper（T-D1 / T-D2）。

设计原则：
- 关键日志携带固定字段 ``subscription_id / source_file_id / run_id / attempts / error_kind / elapsed_ms``，
  便于 ELK / Loki / Loki-querier 按字段聚合分析。
- 沿用 ``app.configure_logging`` 的 ``JsonFormatter``，仅在日志 record 中加 extra，
  JsonFormatter 自动通过 ``record.__dict__`` 读取并写入 JSON。
- 不引入新依赖，仅用标准库 ``logging``。

T-D1 提供事件名常量；T-D2 实现 ``log_event()`` helper。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("aliyundrive_sub.events")


# ====================== 事件名常量 ======================
# 整个项目统一使用这些常量，避免散落字符串。
EVT_TRANSFER_OK = "transfer.ok"             # 单文件转存成功（INFO）
EVT_TRANSFER_RETRY = "transfer.retry"       # 单次重试中（WARN）
EVT_TRANSFER_FAIL = "transfer.fail"         # 单文件最终失败（ERROR）
EVT_TRANSFER_SKIP = "transfer.skip"         # 跳过：已存在 / 锁冲突
EVT_TARGET_MISSING = "target.missing"       # 目标目录预检失败
EVT_RUN_START = "run.start"                 # 本次 run 启动
EVT_RUN_END = "run.end"                     # 本次 run 结束
EVT_RUN_LOCKED = "run.locked"               # 订阅被锁跳过
EVT_RESUME_TASK = "resume.task"             # 启动续跑单条 task
EVT_RESUME_DONE = "resume.done"             # 续跑收尾


def log_event(
    event: str,
    level: int = logging.INFO,
    *,
    subscription_id: Optional[int] = None,
    run_id: Optional[int] = None,
    source_file_id: Optional[str] = None,
    target_file_id: Optional[str] = None,
    source_name: Optional[str] = None,
    attempts: Optional[int] = None,
    error_kind: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    message: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """发出结构化日志，固定字段透传到 JSON。

    Args:
        event: 事件名（用本模块常量）。
        level: logging 级别（INFO / WARNING / ERROR）。
        subscription_id / run_id / source_file_id / target_file_id / source_name:
            业务主键字段；非 None 时写入。
        attempts: 重试次数。
        error_kind: ``TransferErrorKind`` 的字符串值。
        elapsed_ms: 本次操作耗时（毫秒）。
        message: 简短人类可读 message（与 event 名不同），写入 payload["msg"]。
                 注意：Python logging 内部用 ``record.message``，同名 key 会冲突；
                 故此处用 ``msg`` 字段（同样在 JSON 中可见）。
        extra: 其他自定义字段，原样合入日志。
    """
    # Python logging 内置字段白名单（用于过滤，避免与 record.__dict__ 中的内置冲突）
    _RESERVED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        # 防止 log_event 的形参名与内置冲突
        "event", "message",
    }
    payload: dict[str, Any] = {"event": event}
    if subscription_id is not None:
        payload["subscription_id"] = subscription_id
    if run_id is not None:
        payload["run_id"] = run_id
    if source_file_id is not None:
        payload["source_file_id"] = source_file_id
    if target_file_id is not None:
        payload["target_file_id"] = target_file_id
    if source_name is not None:
        payload["source_name"] = source_name
    if attempts is not None:
        payload["attempts"] = attempts
    if error_kind is not None:
        payload["error_kind"] = error_kind
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    if message is not None:
        # 用 detail 字段避开 LogRecord 内置的 msg / message
        payload["detail"] = message
    if extra:
        for k, v in extra.items():
            if k in _RESERVED:
                # 自动改名加前缀，避免覆盖内置 key
                payload[f"x_{k}"] = v
            else:
                payload[k] = v
    # 走 logger.log(level, msg, extra=payload) — payload 会被合并到 record.__dict__
    logger.log(level, event, extra=payload)


class Timer:
    """轻量计时器（与 ``log_event`` 配合使用）。

    用法::
        t = Timer()
        ... do something ...
        log_event(EVT_TRANSFER_OK, elapsed_ms=t.elapsed_ms(), ...)
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)
