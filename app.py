"""程序入口：装配 Config / DB / AliyunClient / Scheduler / Web 并启动。

仅做依赖装配与启动；业务逻辑全在 core/ 与 web/。
`build_services()` 可被测试导入，避免副作用。

T-D1 增补：
- ``TransferRepo`` 任务 / 运行仓储
- ``TargetCache`` 目标目录预检缓存
- ``SubLockManager`` 订阅级并发锁
- ``RetryPolicy`` 默认转存重试策略

T-D4 增补 ``_resume_pending_tasks()`` 后台异步任务（启动续跑）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from config import Settings
from core.aliyun_client import AliyunClient, build_http_session
from core.aria2 import Aria2Client
from core.naming import NamingRule
from core.notifier import NotifierManager, TelegramNotifier
from core.retry import RetryPolicy
from core.scheduler import SchedulerService
from core.sub_lock import SubLockManager
from core.target_cache import TargetCache
from core.token_store import TokenStore
from core.transfer import SubscriptionChecker
from core.transfer_repo import TransferRepo
from db import get_session_local, init_db, init_engine
from web.app import create_app
from web.services import Services

logger = logging.getLogger(__name__)


def configure_logging(level: str = "INFO") -> None:
    """配置结构化 JSON 日志（见 ARCHITECTURE.md 日志规范）。"""

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "module": record.name,
                "msg": record.getMessage(),
            }
            # 透传 extra（如 log_event 写入的 subscription_id / run_id 等）。
            for key, value in record.__dict__.items():
                if key in (
                    "args", "asctime", "created", "exc_info", "exc_text", "filename",
                    "funcName", "levelname", "levelno", "lineno", "message", "module",
                    "msecs", "msg", "name", "pathname", "process", "processName",
                    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
                ):
                    continue
                log[key] = value
            if record.exc_info:
                log["exc"] = self.formatException(record.exc_info)
            return json.dumps(log, ensure_ascii=False)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def build_services(settings: Settings) -> Services:
    """装配全部运行时服务并返回容器。"""
    # 确保相对路径 SQLite 的父目录存在。
    if settings.DATABASE_URL.startswith("sqlite:///"):
        db_path = settings.DATABASE_URL[len("sqlite:///"):]
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    init_engine(settings.DATABASE_URL)
    init_db()
    session_factory = get_session_local()

    # 启动时把 env 中的 TG 通知凭证幂等预种入 KV，使设置页能正确回显 env 配置，
    # 且用户保存其它设置时不会误清空已配的 TG 通知。
    try:
        from models import Setting as _Setting
        with session_factory() as _db:
            def _seed(key, val):
                if not val:
                    return
                if _db.query(_Setting).filter_by(key=key).first() is None:
                    _db.add(_Setting(key=key, value=val))
            if settings.TG_BOT_TOKEN and settings.TG_NOTIFY_CHAT_ID:
                _seed("tg_bot_token", settings.TG_BOT_TOKEN)
                _seed("tg_notify_chat_id", settings.TG_NOTIFY_CHAT_ID)
                if _db.query(_Setting).filter_by(key="tg_notify_enabled").first() is None:
                    _db.add(_Setting(key="tg_notify_enabled", value="true"))
                _db.commit()
    except Exception:
        logger.exception("预种 TG 通知 KV 失败（忽略）")

    http = build_http_session()
    token_store = TokenStore(session_factory, settings.ALIYUNDRIVE_REFRESH_TOKEN)
    client = AliyunClient(settings, http, token_store)
    naming = NamingRule()

    notifier = NotifierManager()
    # T-TG：Telegram Bot 仅用于命中通知；Token 与 chat_id 同时非空才启用。
    if settings.TG_BOT_TOKEN and settings.TG_NOTIFY_CHAT_ID:
        notifier.register(
            TelegramNotifier(settings.TG_BOT_TOKEN, settings.TG_NOTIFY_CHAT_ID)
        )

    aria2 = Aria2Client(
        settings.ARIA2_RPC_URL, settings.ARIA2_RPC_SECRET, settings.ARIA2_RPC_ENABLE
    )

    # ---- T-D1 新增依赖 ----
    transfer_repo = TransferRepo(session_factory)
    target_cache = TargetCache(ttl_seconds=300)
    sub_lock = SubLockManager()
    retry_policy = RetryPolicy(
        max_attempts=3,
        base=1.0,
        cap=8.0,
        retriable_kinds={
            # 默认仅 NETWORK / RATE_LIMITED 可重试；其他业务错误立即失败。
            # T-D2 引入 TransferErrorKind 枚举时保持一致。
            "network",
            "rate_limited",
        },
    )

    checker = SubscriptionChecker(
        client, naming, notifier, aria2, session_factory,
        settings.SHARE_EXPIRE_THRESHOLD_DAYS,
        transfer_repo=transfer_repo,
        target_cache=target_cache,
        sub_lock=sub_lock,
        retry_policy=retry_policy,
    )
    scheduler = SchedulerService(
        checker, session_factory, settings,
        transfer_repo=transfer_repo,
        sub_lock=sub_lock,
        tg_monitor=None,
    )

    # ---- T-TG：Telegram 频道监控自动转存 ----
    # 仅在总开关开启且配置了频道时装配（TG_MONITOR_ENABLED=false 时完全无副作用）。
    # scheduler 需先于 tg_monitor 构造（tg_monitor 注入 scheduler），故先建占位、
    # 再回填 _tg_monitor（SchedulerService.__init__ 已保留 tg_monitor 形参供未来直传）。
    #
    # 启动自动恢复（修复“重启后无法自动监控”）：env 的 TG_MONITOR_* 仅用于容器/命令行
    # 配置；UI「自动启动监控」开关保存在 KV（tg_monitor_enabled / tg_monitor_channels /
    # tg_poll_interval / tg_proxy）。若 env 未开启但 KV 已开启，则把 KV 配置回灌到
    # ``settings.TG_*`` 再据此构造 tg_monitor，确保无需手动点保存即可自动启动监控。
    try:
        from models import Setting as _KVSetting

        with session_factory() as _db:
            def _kv(key: str, default: str = "") -> str:
                row = _db.query(_KVSetting).filter_by(key=key).first()
                return row.value if row else default

            kv_enabled = str(_kv("tg_monitor_enabled", "")).strip().lower() in (
                "1", "true", "yes", "on", "y", "t",
            )
            if not settings.TG_MONITOR_ENABLED and kv_enabled:
                kv_channels = (_kv("tg_monitor_channels", "") or "").strip()
                kv_poll = _kv("tg_poll_interval", "")
                kv_proxy = (_kv("tg_proxy", "") or "").strip()
                if kv_channels:
                    # 回灌到 settings（pydantic v2 实例默认允许属性赋值），
                    # 使 env 构造分支与 tg_monitor 内部均读取到正确的 KV 配置。
                    try:
                        settings.TG_MONITOR_ENABLED = True
                        settings.TG_MONITOR_CHANNELS = kv_channels
                        if kv_poll.strip().isdigit():
                            settings.TG_POLL_INTERVAL = max(60, int(kv_poll))
                        settings.TG_PROXY = kv_proxy
                        logger.info(
                            "已从 KV 恢复 TG 监控配置（自动启动）：channels=%s",
                            kv_channels,
                        )
                    except (AttributeError, TypeError):
                        logger.warning("无法将 KV 监控配置回灌到 settings（跳过自动恢复）")
    except Exception:
        logger.exception("读取 TG 监控 KV 失败（不影响主流程）")

    tg_monitor = None
    if settings.TG_MONITOR_ENABLED and settings.TG_MONITOR_CHANNELS.strip():
        from core.tg_monitor import TGMonitorService

        tg_monitor = TGMonitorService(settings, session_factory, scheduler, notifier)
        scheduler._tg_monitor = tg_monitor

    # ---- 同步管理模块（网盘同步：local + 外部 OpenList/AList）----
    from core.sync import SyncService

    sync_service = SyncService(session_factory, notifier)

    # ---- 装配服务容器 ----
    services = Services(
        settings=settings,
        session_factory=session_factory,
        client=client,
        checker=checker,
        scheduler=scheduler,
        notifier=notifier,
        aria2=aria2,
        naming=naming,
        token_store=token_store,
        transfer_repo=transfer_repo,
        target_cache=target_cache,
        sub_lock=sub_lock,
        retry_policy=retry_policy,
        tg_monitor=tg_monitor,
        sync_service=sync_service,
    )

    # ---- T-D4：启动时后台异步续跑 pending 任务 ----
    try:
        _schedule_resume(services)
    except Exception:
        logger.warning("启动续跑任务调度失败（不影响主流程）", exc_info=True)

    return services


def _schedule_resume(services: Services) -> None:
    """在 ``build_services`` 末尾调用：异步跑一次启动续跑，不阻塞 Web 启动。

    ``asyncio.create_task`` 需要运行中的事件循环；Flask ``app.run`` 是同步入口，
    但在「调试/单测」场景下没有 event loop 可用，因此采用「线程 + 短命 loop」策略。
    """
    import threading

    def _runner() -> None:
        try:
            asyncio.run(_resume_pending_tasks(services))
        except Exception:
            logger.warning("启动续跑任务异常（不影响主进程）", exc_info=True)

    t = threading.Thread(target=_runner, name="resume-pending", daemon=True)
    t.start()


async def _resume_pending_tasks(services: Services) -> None:
    """T-D4：遍历 active 订阅下 status='pending' 且 attempts<max 的 task，逐条 re-run。

    行为（DESIGN §5.3）：
    - 每个 sub 仍走 ``SubLockManager``（避免与 Web 手动 trigger 撞车）
    - 按 ``next_retry_at`` 排序；未到时间的 task 跳过
    - ``_transfer_one`` 套用同一 ``with_retry`` 装饰器（即「重跑 = 一次新的 _transfer_one」）
    - 异常单独捕获，不影响主进程
    """
    from core.log_fields import EVT_RESUME_DONE, log_event
    from core.transfer_repo import TASK_PENDING, TransferRepo as TR

    repo: TR = services.transfer_repo
    if repo is None:
        logger.info("启动续跑：transfer_repo 未注入，跳过")
        return
    lock_mgr = services.sub_lock
    max_attempts = 3
    if services.retry_policy is not None:
        max_attempts = services.retry_policy.max_attempts

    with services.session_factory() as db:
        pending = repo.list_all_pending(db, max_attempts=max_attempts)

    log_event(
        EVT_RESUME_DONE,
        level=logging.INFO,
        extra={"resume_count": len(pending)},
    )
    if not pending:
        return

    from db import utc_now
    from models import Subscription as Sub, TransferTask as TT

    resumed = failed = 0
    for task in pending:
        # next_retry_at 未到则跳过
        if task.next_retry_at and task.next_retry_at > utc_now():
            continue
        # 抢订阅锁
        if lock_mgr is not None:
            lock, acquired = await lock_mgr.try_acquire(task.subscription_id)
            if not acquired:
                continue
            try:
                await _resume_one(services, task)
                resumed += 1
            except Exception:
                failed += 1
                logger.exception("续跑 task %s 失败", task.id)
            finally:
                lock_mgr.release(task.subscription_id)
        else:
            try:
                await _resume_one(services, task)
                resumed += 1
            except Exception:
                failed += 1
                logger.exception("续跑 task %s 失败", task.id)
    log_event(
        EVT_RESUME_DONE,
        level=logging.INFO,
        extra={"resumed": resumed, "failed": failed},
    )


async def _resume_one(services: Services, task) -> None:
    """续跑单条 task：重新解析分享 + 调 save_file + finish_task。"""
    from core.log_fields import EVT_RESUME_TASK, log_event
    from models import Subscription
    from sqlalchemy.orm import Session

    with services.session_factory() as db:
        sub = db.get(Subscription, task.subscription_id)
    if sub is None or sub.status != "active":
        # 订阅失效，标 failed
        from core.transfer_repo import TASK_FAILED
        with services.session_factory() as db:
            services.transfer_repo.finish_task(
                db, task.id, TASK_FAILED,
                last_error="subscription not active", error_kind="unknown",
                commit=True,
            )
        return
    log_event(
        EVT_RESUME_TASK,
        level=logging.INFO,
        subscription_id=task.subscription_id,
        run_id=task.run_id,
        source_file_id=task.source_file_id,
        source_name=task.source_name,
        attempts=task.attempts,
    )
    # 解析分享 + 调 _transfer_one（同步函数包在线程池中跑）
    import asyncio
    try:
        await asyncio.to_thread(
            services.checker.check, sub, "scheduled",
        )
    except Exception:
        logger.exception("续跑单条异常 (sub=%s task=%s)", sub.id, task.id)


def _init_sync_module(services: Services) -> None:
    """确保同步管理模块的内置 TaoSync 引擎存在，并恢复运行作业。"""
    from core.sync.storage_engine_bootstrap import ensure_builtin_engine
    from db import utc_now

    sync = services.sync_service
    if sync is None:
        return
    # 幂等创建内置 taosync 引擎（protected=1，UI 不可删除）。
    ensure_builtin_engine(services.session_factory)
    # 启动时恢复异常终止状态的任务并启动所有启用作业。
    sync.init_jobs()


def main() -> None:
    settings = Settings()
    configure_logging(settings.LOG_LEVEL)
    services = build_services(settings)
    app = create_app(services)
    try:
        services.scheduler.start()
    except Exception:
        logger.warning("调度器启动失败（仍可通过 Web 手动触发检查）", exc_info=True)
    # 启动 TG 频道监控 Bot 实时监听（若已配置 TG_BOT_TOKEN 与监控频道）
    if services.tg_monitor is not None:
        try:
            services.tg_monitor.start()
        except Exception:
            logger.warning("TG 频道监控 Bot 启动失败（不影响 Web）", exc_info=True)
    # 注册全局订阅状态巡检 job（SubStatus）：仅扫描 active 订阅做链接探测 + 完结计时。
    try:
        services.scheduler.register_substatus_poll(services)
    except Exception:
        logger.warning("注册 substatus_poll 巡检任务失败（不影响订阅调度）", exc_info=True)
    # 初始化同步管理模块：确保内置 TaoSync 引擎存在，并恢复/启动所有启用作业。
    try:
        _init_sync_module(services)
    except Exception:
        logger.warning("同步管理模块初始化失败（不影响主流程）", exc_info=True)
    logger.info("Web 启动于 %s:%s", settings.WEB_HOST, settings.WEB_PORT)
    # 生产请用 gunicorn -w 1（单 worker，避免多调度器）；此处用 Flask 内置服务器做 MVP。
    app.run(host=settings.WEB_HOST, port=settings.WEB_PORT, use_reloader=False)


if __name__ == "__main__":
    main()
