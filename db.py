"""数据库引擎、会话工厂与建表引导。

遵循 ARCHITECTURE.md 共享约定：SQLAlchemy 2.0 + SQLite 单库。
所有时间戳以 UTC(naive) 落库，Web 展示时按 TZ 转换。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

# 全局引擎与会话工厂（进程内单例），由 init_engine() 初始化。
_engine = None
_SessionLocal = None


def utc_now() -> datetime:
    """返回当前 UTC 时间（naive），便于 SQLite 稳定存储。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有 ORM 实体的声明基类。"""


def init_engine(database_url: str) -> None:
    """创建（或重建）数据库引擎与会话工厂。

    Args:
        database_url: SQLAlchemy 连接串，例如 ``sqlite:///data/app.db``。
    """
    global _engine, _SessionLocal
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        # 允许跨线程访问（APScheduler 后台线程 + Flask 请求线程共享）。
        connect_args = {"check_same_thread": False}
    _engine = create_engine(
        database_url, connect_args=connect_args, future=True, pool_pre_ping=True
    )
    _SessionLocal = sessionmaker(
        bind=_engine, autoflush=False, autocommit=False, future=True
    )
    logger.info("数据库引擎已初始化: %s", database_url)


def get_engine():
    """获取已初始化的引擎（未初始化则抛错）。"""
    if _engine is None:
        raise RuntimeError("数据库引擎未初始化，请先调用 init_engine()。")
    return _engine


def get_session_local() -> sessionmaker:
    """获取会话工厂（未初始化则抛错）。"""
    if _SessionLocal is None:
        raise RuntimeError("会话工厂未初始化，请先调用 init_engine()。")
    return _SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """数据库会话上下文管理器：自动提交 / 回滚 / 关闭。"""
    factory = get_session_local()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """创建全部表（幂等）。需在 ORM 模型导入后调用。"""
    import models  # noqa: F401  # 触发 ORM 元数据注册到 Base.metadata
    import models_sync  # noqa: F401  # 同步管理模块 ORM 元数据注册
    from sqlalchemy import text

    Base.metadata.create_all(get_engine())
    _auto_migrate()
    logger.info("数据库表结构已就绪")


def _auto_migrate() -> None:
    """轻量 idempotent 迁移：补齐 create_all 不处理的列添加。

    当前处理：
      - subscriptions.target_drive_type  VARCHAR(32) DEFAULT ''
        （记录 target_folder_id 所属盘类型：default/resource/backup）
      - subscriptions.rename_mode / rename_prefix / rename_suffix
        （重命名规则三列，供订阅页可配置重命名）
      - subscriptions.source  VARCHAR(32) DEFAULT 'manual'
        （订阅来源：manual / tg_monitor）
      - tg_monitor_state 表（Telegram 频道监控去重状态，T-TG）
      - 剥离 tg_monitor 订阅名的历史 "TG:" 前缀（幂等 UPDATE，执行一次后无匹配行）
    """
    from sqlalchemy import text

    with get_engine().begin() as conn:
        cols = {
            r[1]
            for r in conn.execute(text("PRAGMA table_info(subscriptions)")).fetchall()
        }
        if "target_drive_type" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN target_drive_type VARCHAR(32) DEFAULT ''"
                )
            )
            logger.info("已迁移: subscriptions.target_drive_type")

        # 重命名规则三列：用 inspect 检查列是否存在，不存在才 ALTER（幂等）。
        for col_name, col_type in (
            ("rename_mode", "VARCHAR(32) DEFAULT ''"),
            ("rename_prefix", "VARCHAR(128) DEFAULT ''"),
            ("rename_suffix", "VARCHAR(128) DEFAULT ''"),
        ):
            if col_name not in cols:
                conn.execute(
                    text(
                        f"ALTER TABLE subscriptions ADD COLUMN {col_name} {col_type}"
                    )
                )
                logger.info("已迁移: subscriptions.%s", col_name)

        # T-TG：订阅来源列（与 ORM 默认 'manual' 一致）。
        if "source" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'manual'"
                )
            )
            logger.info("已迁移: subscriptions.source")

        # SubStatus：订阅状态巡检所需两列（朴素 UTC / int）。
        #  - last_transfer_at：最后成功转存时间，空则回退 created_at 算完结超时。
        #  - link_fail_count：链接探测失败累计次数，达阈值 → 链接失效。
        # 幂等：沿用 cols 守卫写法，仅当列不存在时才 ALTER。
        for col_name, col_type in (
            ("last_transfer_at", "DATETIME"),
            ("link_fail_count", "INTEGER DEFAULT 0"),
        ):
            if col_name not in cols:
                conn.execute(
                    text(
                        f"ALTER TABLE subscriptions ADD COLUMN {col_name} {col_type}"
                    )
                )
                logger.info("已迁移: subscriptions.%s", col_name)

        # T-TG：Telegram 频道监控去重状态表（与 models.TGMonitorState 字段一致）。
        # ORM 经 create_all 已建表时此语句为 no-op；用于既有库补齐。
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tg_monitor_state ("
                " id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
                " channel VARCHAR(255) NOT NULL,"
                " last_message_id INTEGER,"
                " processed_links TEXT,"
                " updated_at DATETIME,"
                " UNIQUE (channel)"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tg_monitor_state_channel "
                "ON tg_monitor_state (channel)"
            )
        )

        # T-TG 日志模块：监控运行日志表（与 models.TGMonitorLog 字段一致）。
        # ORM 经 create_all 已建表时此语句为 no-op；用于既有库补齐。
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tg_monitor_logs ("
                " id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
                " channel VARCHAR(255) NOT NULL,"
                " message_id INTEGER,"
                " event_type VARCHAR(32),"
                " message_type VARCHAR(32),"
                " status VARCHAR(32),"
                " link TEXT,"
                " detail TEXT,"
                " created_at DATETIME"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tg_monitor_logs_channel "
                "ON tg_monitor_logs (channel)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tg_monitor_logs_created_at "
                "ON tg_monitor_logs (created_at)"
            )
        )

        # T-TG：日志表新增 content 列（频道推送具体内容原文），供「频道日记」时间轴展示。
        # 幂等：仅当列不存在时才 ALTER（既有库补齐，新建库由 create_all 已含此列）。
        log_cols = {
            r[1]
            for r in conn.execute(
                text("PRAGMA table_info(tg_monitor_logs)")
            ).fetchall()
        }
        if "content" not in log_cols:
            conn.execute(
                text("ALTER TABLE tg_monitor_logs ADD COLUMN content TEXT DEFAULT ''")
            )
            logger.info("已迁移: tg_monitor_logs.content")

        # 剥离 TG 监控自动订阅的历史 "TG:" 前缀（幂等：执行一次后无匹配行）。
        # SQLite 的 SUBSTR 是 1-based，SUBSTR(name, 4) 正好跳过 "TG:"（3 个字符）。
        conn.execute(
            text(
                "UPDATE subscriptions SET name = SUBSTR(name, 4) "
                "WHERE source = 'tg_monitor' AND name LIKE 'TG:%'"
            )
        )

        # 下线「识别词监控」功能：清理遗留 KV（孤儿数据），幂等可重复执行。
        conn.execute(
            text(
                "DELETE FROM settings WHERE key = 'tg_recognition_enabled' "
                "OR key = 'tg_recognition_words' "
                "OR key LIKE 'tg_recognition_words:%'"
            )
        )

        # 下线「默认转存目录」功能：清理遗留 KV（孤儿数据），幂等可重复执行。
        conn.execute(
            text(
                "DELETE FROM settings WHERE key = 'tg_default_target_folder_id' "
                "OR key = 'tg_default_target_folder_path' "
                "OR key = 'tg_default_target_folder_drive_type'"
            )
        )
