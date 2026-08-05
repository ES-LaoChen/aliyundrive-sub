"""订阅状态巡检轮询器（SubStatus）。

独立全局巡检 job：仅扫描 ``status='active'`` 订阅，做两件事：

1. **超时判定**：``最后成功转存时间（last_transfer_at 或回退 created_at）``
   距今超过 ``COMPLETED_TIMEOUT_DAYS``（30）天 → 转「已完结」(``completed``) 并解注册。
2. **链接探测**：复用 ``client.get_share_info(share_id)`` 探测链接有效性；
   任何异常（``ShareExpiredError`` / 4xx / 429 / 超时 / 网络）均计入失败，
   累计达阈值 → 转「链接失效」(``pending_update``) 并解注册；探测成功则失败计数清零。

运行参数全部从 ``Setting`` KV 表实时读取（天然热更新）；仅 job 周期
``sub_check_interval`` 变更时需经 ``scheduler.reschedule_substatus_poll`` 重排。

并发：``ThreadPoolExecutor``（I/O 密集）；``substatus_poll_wait_seconds`` 做提交节流
（每提交一个任务后 ``time.sleep(wait)``，``as_completed`` 收尾）；并发关闭时顺序执行
并在每项之间 ``sleep``。

> 设计约束：不引入新第三方依赖，仅用标准库 ``concurrent.futures`` / ``time`` / ``datetime``；
> ``_get_kv`` 本地定义，避免与 web 蓝图循环依赖。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from db import utc_now
from models import Setting, Subscription

logger = logging.getLogger(__name__)

# SubStatus KV 键名（与设置页 / schemas 对齐）。
KV_LINK_FAIL_THRESHOLD = "link_fail_threshold"
KV_SUB_CHECK_INTERVAL = "sub_check_interval"
KV_CONCURRENCY_ENABLED = "substatus_concurrency_enabled"
KV_CONCURRENCY_WORKERS = "substatus_concurrency_workers"
KV_POLL_WAIT_SECONDS = "substatus_poll_wait_seconds"

# 默认 KV 值（未配置时回退）。
_DEFAULTS: dict[str, str] = {
    KV_LINK_FAIL_THRESHOLD: "3",
    KV_SUB_CHECK_INTERVAL: "3600",
    KV_CONCURRENCY_ENABLED: "false",
    KV_CONCURRENCY_WORKERS: "3",
    KV_POLL_WAIT_SECONDS: "2",
}

# 已完结超时阈值（天）：超过此天数无成功转存 → 已完结。
_COMPLETED_TIMEOUT_DAYS = 30

# job 周期下限（秒），与 TG_POLL_INTERVAL 的 clamp 对齐。
_MIN_INTERVAL_SECONDS = 60


def _get_kv(db, key: str, default: str = "") -> str:
    """从 ``Setting`` KV 表读取配置（与 ``settings_bp._get_kv`` 同语义的本地辅助）。

    本地定义一份，避免与 web 蓝图形成循环依赖；poller 内部统一用字符串读写，
    业务层再解析为 int / bool。

    Args:
        db: 数据库会话。
        key: KV 键名。
        default: 键不存在时回退的默认值。

    Returns:
        键对应的字符串值；不存在返回 ``default``。
    """
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else default


@dataclass
class PollConfig:
    """一次巡检的运行时配置快照。

    由 ``load_poll_config`` 每轮从 ``Setting`` 表读取并解析，业务层直接消费。
    """

    threshold: int = 3            # 链接失败阈值
    interval: int = 3600          # 巡检周期（秒，clamp >= 60）
    concurrency_enabled: bool = False  # 是否开启并发检查
    workers: int = 3              # 并发 worker 数（仅 enabled 生效）
    wait: int = 2                 # 中途等待时间（秒，节流防爬虫）


def load_poll_config(session_factory: Callable[[], Any]) -> PollConfig:
    """每轮从 ``Setting`` KV 表读取 5 个配置项，解析并兜底默认。

    Args:
        session_factory: ``Callable[[], Session]``，返回数据库会话。

    Returns:
        ``PollConfig`` 运行时配置快照。
    """
    with session_factory() as db:
        raw_threshold = _get_kv(db, KV_LINK_FAIL_THRESHOLD, _DEFAULTS[KV_LINK_FAIL_THRESHOLD])
        raw_interval = _get_kv(db, KV_SUB_CHECK_INTERVAL, _DEFAULTS[KV_SUB_CHECK_INTERVAL])
        raw_conc = _get_kv(db, KV_CONCURRENCY_ENABLED, _DEFAULTS[KV_CONCURRENCY_ENABLED])
        raw_workers = _get_kv(db, KV_CONCURRENCY_WORKERS, _DEFAULTS[KV_CONCURRENCY_WORKERS])
        raw_wait = _get_kv(db, KV_POLL_WAIT_SECONDS, _DEFAULTS[KV_POLL_WAIT_SECONDS])

    # 失败阈值：int 解析失败回退默认。
    try:
        threshold = int(raw_threshold)
    except (ValueError, TypeError):
        logger.warning("link_fail_threshold 非法(%r)，回退 %s", raw_threshold, _DEFAULTS[KV_LINK_FAIL_THRESHOLD])
        threshold = int(_DEFAULTS[KV_LINK_FAIL_THRESHOLD])

    # 巡检周期：int 解析失败或低于下限均回退默认并 clamp。
    try:
        interval = max(_MIN_INTERVAL_SECONDS, int(raw_interval))
    except (ValueError, TypeError):
        logger.warning("sub_check_interval 非法(%r)，回退 %s", raw_interval, _DEFAULTS[KV_SUB_CHECK_INTERVAL])
        interval = int(_DEFAULTS[KV_SUB_CHECK_INTERVAL])

    # 并发开关：字符串 "true"（不区分大小写）为真。
    concurrency_enabled = str(raw_conc).strip().lower() == "true"

    # 并发数量：至少 1，int 解析失败回退默认。
    try:
        workers = max(1, int(raw_workers))
    except (ValueError, TypeError):
        logger.warning("substatus_concurrency_workers 非法(%r)，回退 %s", raw_workers, _DEFAULTS[KV_CONCURRENCY_WORKERS])
        workers = int(_DEFAULTS[KV_CONCURRENCY_WORKERS])

    # 中途等待：int 解析失败回退默认（允许 0 = 不等待）。
    try:
        wait = int(raw_wait)
    except (ValueError, TypeError):
        logger.warning("substatus_poll_wait_seconds 非法(%r)，回退 %s", raw_wait, _DEFAULTS[KV_POLL_WAIT_SECONDS])
        wait = int(_DEFAULTS[KV_POLL_WAIT_SECONDS])

    return PollConfig(
        threshold=threshold,
        interval=interval,
        concurrency_enabled=concurrency_enabled,
        workers=workers,
        wait=wait,
    )


def load_sub_check_interval(session_factory: Callable[[], Any]) -> int:
    """仅读取巡检周期（供 scheduler 注册 job 使用），clamp >= 60。

    Args:
        session_factory: ``Callable[[], Session]``，返回数据库会话。

    Returns:
        巡检周期秒数（>= 60）。
    """
    with session_factory() as db:
        raw = _get_kv(db, KV_SUB_CHECK_INTERVAL, _DEFAULTS[KV_SUB_CHECK_INTERVAL])
    try:
        return max(_MIN_INTERVAL_SECONDS, int(raw))
    except (ValueError, TypeError):
        return int(_DEFAULTS[KV_SUB_CHECK_INTERVAL])


def check_timeout(sub: Subscription, now: Optional[datetime] = None) -> bool:
    """判断订阅是否已超过阈值天数无成功转存。

    基准时间 = ``last_transfer_at`` 或回退 ``created_at``；两者皆空则视为未超时
    （避免新订阅立即被完结）。

    Args:
        sub: 订阅对象（需含 last_transfer_at / created_at）。
        now: 基准「现在」时间（测试可注入）；默认取 ``utc_now()``。

    Returns:
        是否已超过完结超时阈值。
    """
    now = now or utc_now()
    base = sub.last_transfer_at or sub.created_at
    if base is None:
        return False
    return now - base > timedelta(days=_COMPLETED_TIMEOUT_DAYS)


def probe_link(services, sub: Subscription) -> bool:
    """探测订阅分享链接是否有效（复用 ``client.get_share_info``）。

    探测路径与 ``core/transfer.py`` 一致：先 ``resolve_share`` 取 ``share_id``，
    再 ``get_share_info(share_id)`` 校验。任何异常
    （``ShareExpiredError`` / 4xx / 429 / 超时 / 网络）均视为失败 → ``False``。

    Args:
        services: 运行时服务容器（需含 ``client``）。
        sub: 订阅对象（需含 ``share_url``）。

    Returns:
        ``True`` 表示链接有效；``False`` 表示探测失败。
    """
    from core.types import ShareExpiredError

    client = services.client
    try:
        share_id, _token = client.resolve_share(sub.share_url)
        client.get_share_info(share_id)
        return True
    except ShareExpiredError:
        # 分享已失效（链接失效的典型情形）。
        logger.debug("链接探测失效(ShareExpired): sub=%s", getattr(sub, "id", None))
        return False
    except Exception as exc:  # noqa: BLE001  # 覆盖 4xx/429/超时/网络等全类异常
        logger.debug("链接探测失败: sub=%s, err=%s", getattr(sub, "id", None), exc)
        return False


def _unregister(scheduler, sub_id: int) -> None:
    """安全解注册 per-sub 转存 job（scheduler 可能为 None 或方法缺失）。"""
    if scheduler is None:
        return
    try:
        scheduler.unregister(sub_id)
    except Exception:
        logger.debug("解注册订阅 #%s 失败（忽略）", sub_id)


def _notify(services, title: str, content: str, level: str = "info") -> None:
    """best-effort 发送状态变更通知（任何异常静默忽略，避免影响巡检主流程）。"""
    notifier = getattr(services, "notifier", None)
    if notifier is None:
        return
    try:
        notifier.send(title, content, level)
    except Exception:
        logger.debug("发送状态变更通知失败（忽略）: %s", title)


def process_sub(services, sub: Subscription, threshold: int) -> None:
    """处理单个 active 订阅：先超时判定，再链接探测，写状态/计数/解注册。

    设计要点（决议 #9 / 设计 R4）：**先判超时后探测**，避免对即将完结的订阅重复计数。

    线程安全：每个 worker 独立开会话、独立行提交（与现有 ``db.check_same_thread=False``
    配合），不跨线程共享会话。``sub`` 仅用于读取 ``id``，实际读写在独立会话内完成。

    Args:
        services: 运行时服务容器（需含 ``session_factory`` / ``scheduler`` / ``notifier``）。
        sub: 订阅对象（仅使用其 ``id`` 重新查询；可为 detached 对象）。
        threshold: 链接失败阈值。
    """
    sub_id = sub.id
    scheduler = getattr(services, "scheduler", None)
    try:
        with services.session_factory() as db:
            fresh = db.get(Subscription, sub_id)
            if fresh is None or fresh.status != "active":
                # 订阅已不存在或被其他流程改态，跳过。
                return

            # ---- 先判超时（R4 竞态规避）----
            if check_timeout(fresh):
                fresh.status = "completed"
                db.commit()
                _unregister(scheduler, sub_id)
                logger.info("订阅 #%s 超过 %d 天无成功转存 → 已完结", sub_id, _COMPLETED_TIMEOUT_DAYS)
                _notify(
                    services, "订阅已完结",
                    f"订阅「{fresh.name}」# {sub_id} 超过 {_COMPLETED_TIMEOUT_DAYS} 天无成功转存，已自动标记为已完结。",
                    "info",
                )
                return

            # ---- 链接探测 ----
            ok = probe_link(services, fresh)
            if ok:
                # 探测成功：失败计数清零。
                fresh.link_fail_count = 0
                logger.debug("订阅 #%s 链接探测成功，失败计数清零", sub_id)
            else:
                # 探测失败：计数 +1；达阈值 → 链接失效 + 解注册。
                fresh.link_fail_count = (fresh.link_fail_count or 0) + 1
                logger.debug("订阅 #%s 链接探测失败，累计计数=%s", sub_id, fresh.link_fail_count)
                if fresh.link_fail_count >= threshold:
                    fresh.status = "pending_update"
                    db.commit()
                    _unregister(scheduler, sub_id)
                    logger.warning(
                        "订阅 #%s 链接探测失败达阈值 %s → 链接失效", sub_id, threshold
                    )
                    _notify(
                        services, "订阅链接失效",
                        f"订阅「{fresh.name}」# {sub_id} 链接探测失败达 {threshold} 次，已标记为「链接失效」，请替换分享链接。",
                        "warn",
                    )
                    return
            db.commit()
    except Exception:
        logger.exception("处理订阅 #%s 异常（已忽略）", sub_id)


def run_poll(services) -> None:
    """``substatus_poll`` 巡检 job 入口（由 APScheduler 定时触发）。

    流程：读配置 → 查 active 订阅 → 并发/顺序处理（先超时后探测）→ 提交。

    Args:
        services: 运行时服务容器（需含 ``session_factory`` / ``scheduler`` / ``client``）。
    """
    cfg = load_poll_config(services.session_factory)
    try:
        with services.session_factory() as db:
            subs = db.query(Subscription).filter(Subscription.status == "active").all()
    except Exception:
        logger.exception("巡检查询 active 订阅失败")
        return

    if not subs:
        logger.debug("本轮巡检无 active 订阅，跳过")
        return

    sub_count = len(subs)
    logger.info("开始状态巡检，待处理 active 订阅 %d 个（并发=%s, workers=%s, wait=%s）",
                sub_count, cfg.concurrency_enabled, cfg.workers, cfg.wait)

    if cfg.concurrency_enabled:
        # 并发模式：提交节流（每提交一个任务后 sleep(wait)），as_completed 收尾。
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as ex:
            futures = []
            for s in subs:
                fut = ex.submit(process_sub, services, s, cfg.threshold)
                futures.append(fut)
                if cfg.wait > 0:
                    time.sleep(cfg.wait)
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    logger.exception("巡检子任务异常（已忽略）")
    else:
        # 顺序模式：每项处理后 sleep(wait)。
        for s in subs:
            process_sub(services, s, cfg.threshold)
            if cfg.wait > 0:
                time.sleep(cfg.wait)

    logger.info("状态巡检完成")
