"""调度服务：封装 APScheduler。

- 按订阅注册周期任务（IntervalTrigger），支持动态增删与手动 trigger_once。
- 每个 job 仅持有订阅 id，运行时再查库，保证读到的 status / interval 是最新的。
- 单订阅 ``max_instances=1`` + ``coalesce``，避免重叠与积压。

T-D1 增补：构造接受 ``transfer_repo`` / ``sub_lock``（保持向前兼容）。
T-D4 增补：``trigger_once`` / ``_check_one`` 入口前抢 ``SubLockManager`` 锁；
           抢失败建 ``runs(skipped_locked)`` 并 INFO 日志。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from apscheduler.schedulers.background import BackgroundScheduler

from config import Settings
from core.sub_lock import SubLockManager
from core.transfer import SubscriptionChecker
from core.transfer_repo import RUN_LOCKED, TransferRepo
from models import Subscription

logger = logging.getLogger(__name__)


def parse_interval(value) -> int:
    """解析周期配置为秒数。

    支持：
      - ``@every 1h`` / ``30m`` / ``1d``（h=小时, m=分钟, d=天）
      - 纯数字秒数（如 ``3600``）
    非法值回退为 3600 秒。
    """
    if value is None:
        return 3600
    text = str(value).strip()
    if text.startswith("@every"):
        rest = text[len("@every"):].strip()
        if not rest:
            return 3600
        if rest.endswith("h"):
            return int(rest[:-1]) * 3600
        if rest.endswith("m"):
            return int(rest[:-1]) * 60
        if rest.endswith("d"):
            return int(rest[:-1]) * 86400
        return int(rest)
    return int(text)


class SchedulerService:
    """APScheduler 封装。"""

    def __init__(
        self,
        checker: SubscriptionChecker,
        session_factory: Callable[[], Session],
        settings: Settings,
        transfer_repo: Optional[TransferRepo] = None,
        sub_lock: Optional[SubLockManager] = None,
        tg_monitor: Optional[Any] = None,
    ) -> None:
        self._checker = checker
        self._session_factory = session_factory
        self._settings = settings
        self._sched = BackgroundScheduler(timezone=settings.TZ)
        self._repo = transfer_repo
        self._sub_lock = sub_lock
        # T-TG：Telegram 频道监控服务（可为 None，表示未启用）。
        self._tg_monitor = tg_monitor
        # 暴露底层 APScheduler 实例，供 UI / 状态查询（如 get_job("tg_monitor_poll")）。
        self.scheduler = self._sched

    # ----- 生命周期 -----
    def start(self) -> None:
        with self._session_factory() as db:
            subs = db.query(Subscription).all()
        for sub in subs:
            self.register(sub)
        self._sched.start()
        # T-TG：调度器启动后注册频道监控轮询任务（函数内懒导入避免 import 级耦合）。
        # 注意：只要 tg_monitor 已构造就注册 job；poll_all() 自身有 enabled() 早退，
        # 因此 UI 开关免重启即可生效（无需随 enabled 状态增删 job）。
        if self._tg_monitor is not None:
            try:
                from core.tg_monitor import TGMonitorService  # noqa: F401  # 校验可导入

                interval = max(60, int(self._settings.TG_POLL_INTERVAL))
                self._sched.add_job(
                    self._tg_monitor.poll_all,
                    "interval",
                    seconds=interval,
                    id="tg_monitor_poll",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                logger.info("已注册 TG 监控轮询任务，周期 %s 秒", interval)
            except Exception:
                logger.exception("注册 TG 监控轮询任务失败（不影响订阅调度）")
        logger.info("调度器已启动，已注册 %d 个订阅任务", len(self._sched.get_jobs()))

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
            logger.info("调度器已停止")

    # ----- SubStatus 全局巡检 job（T-SubStatus） -----
    def register_substatus_poll(self, services) -> None:
        """注册全局 ``substatus_poll`` 巡检 job（在 ``scheduler.start()`` 之后调用）。

        周期 = KV ``sub_check_interval``（clamp >= 60），仅扫描 ``active`` 订阅；
        与 per-sub 转存 job 互不替代。注册方式与 ``tg_monitor_poll`` 完全对齐。

        Args:
            services: 运行时服务容器（需含 ``session_factory`` 与 ``client``）。
        """
        from core.substatus_poller import load_sub_check_interval, run_poll

        # 暂存 services，供 reschedule_substatus_poll 复用（无需再次传入）。
        self._services = services
        interval = max(60, load_sub_check_interval(services.session_factory))
        self._sched.add_job(
            run_poll,
            "interval",
            seconds=interval,
            id="substatus_poll",
            args=[services],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("已注册 substatus_poll 巡检任务，周期 %s 秒", interval)

    def reschedule_substatus_poll(self, session_factory) -> None:
        """设置页保存 ``sub_check_interval`` 后热更新 job 周期（无需重启）。

        仅当调度器已运行且此前已 ``register_substatus_poll`` 时生效。

        Args:
            session_factory: ``Callable[[], Session]``，返回数据库会话（供重新读取周期）。
        """
        services = getattr(self, "_services", None)
        if self._sched.running and services is not None:
            self.register_substatus_poll(services)

    # ----- 动态管理 -----
    def register(self, sub: Subscription) -> None:
        """为订阅注册（或更新）周期任务；非 active 状态不注册。"""
        if sub.status != "active":
            return
        secs = parse_interval(sub.interval) or 3600
        self._sched.add_job(
            self._check_one,
            "interval",
            seconds=secs,
            id=f"sub_{sub.id}",
            replace_existing=True,
            args=[sub.id],
            max_instances=1,
            coalesce=True,
        )
        logger.info("已注册订阅任务 sub_%s，周期 %s 秒", sub.id, secs)

    def unregister(self, sub_id: int) -> None:
        job_id = f"sub_{sub_id}"
        if self._sched.get_job(job_id):
            self._sched.remove_job(job_id)
            logger.info("已移除订阅任务 %s", job_id)

    # ----- 触发入口（T-D4 加锁 / skipped_locked run） -----
    def trigger_once(self, sub_id: int) -> None:
        """手动立即检查一次（Web「立即检查」按钮）。

        T-D4 行为：
        1. 抢 sub_lock（in-process 异步锁，瞬时非阻塞）。
           抢失败 → 建一条 ``runs(skipped_locked)``，发 INFO 通知，立即返回。
        2. 抢成功 → 跑 ``checker.check(sub)``，finally 释放锁。
        """
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
        if not sub:
            logger.warning("trigger_once: 订阅 %s 不存在", sub_id)
            return

        # ----- 加锁 -----
        if self._sub_lock is not None:
            lock, acquired = self._run_async(self._sub_lock.try_acquire(sub_id))
            if not acquired:
                self._record_locked_run(sub)
                return
            try:
                self._checker.check(sub, run_mode="manual")
            finally:
                self._sub_lock.release(sub_id)
        else:
            # 无锁管理器：保持旧行为
            self._checker.check(sub, run_mode="manual")

    # ----- 内部 -----
    def _check_one(self, sub_id: int) -> None:
        """APScheduler 触发的入口（与 trigger_once 一致的锁逻辑）。"""
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
        if not sub or sub.status != "active":
            self.unregister(sub_id)
            return

        if self._sub_lock is not None:
            lock, acquired = self._run_async(self._sub_lock.try_acquire(sub_id))
            if not acquired:
                self._record_locked_run(sub)
                return
            try:
                self._checker.check(sub, run_mode="scheduled")
            finally:
                self._sub_lock.release(sub_id)
        else:
            try:
                self._checker.check(sub, run_mode="scheduled")
            except Exception:
                logger.exception("订阅 %s 检查失败", sub_id)

    def _record_locked_run(self, sub: Subscription) -> None:
        """订阅被锁跳过时建一条 ``runs(skipped_locked)`` 并发 INFO 通知。"""
        if self._repo is None:
            logger.info("订阅 #%s 正在被其他流程占用，跳过本次触发", sub.id)
            return
        try:
            with self._session_factory() as db:
                run = self._repo.start_run(db, sub.id, run_mode="manual")
                self._repo.finish_run(
                    db, run.id, RUN_LOCKED,
                    summary={"added": 0, "skipped": 0, "failed": 0, "pending": 0},
                    commit=True,
                )
            self._checker._notifier.send(  # noqa: SLF001  # 复用 checker 的 notifier
                "订阅被锁跳过",
                f"订阅「{sub.name}」#{sub.id} 正在被其他流程占用，本次触发已跳过。",
                "info",
            )
            logger.info("订阅 #%s 锁冲突，建 skipped_locked run #%s", sub.id, run.id)
        except Exception:
            logger.exception("记录 skipped_locked run 失败（不影响主流程）")

    @staticmethod
    def _run_async(coro):
        """在同步上下文（APScheduler 后台线程）中跑一次短协程。

        APScheduler 的 ``BackgroundScheduler`` 默认在线程中跑 job，没有运行事件循环；
        策略：尝试获取当前线程的事件循环；没有则新建、用完即关（仅适用短小的 coroutine）。
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 同线程已有 loop 在跑（不太可能，但兜底）：用 run_coroutine_threadsafe 不可行
                # 退化为新建短命 loop
                new_loop = asyncio.new_event_loop()
                try:
                    return new_loop.run_until_complete(coro)
                finally:
                    new_loop.close()
        except RuntimeError:
            pass
        # 标准路径：新建 loop 跑这一次
        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
