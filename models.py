"""ORM 实体：Subscription / TransferRecord / Setting / Token / TransferTask / Run。

字段与 ARCHITECTURE.md 类图保持一致。
注意：原类图中的 ``Settings`` 实体在本实现中命名为 ``Setting``（表名仍为
``settings``），以避免与 ``config.Settings``（pydantic 配置）命名冲突。

T-D1 新增 ``TransferTask``（单文件粒度任务表，承载 pending/running/success/failed/skipped 状态机）
与 ``Run``（运行级记录表，对应 PRD §3.2 字段）。老 ``TransferRecord`` 保留为「最终结果历史」，
新表与老表并存：成功/跳过的 task 同步写一条 TransferRecord 保持既有去重逻辑不破。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base, utc_now


class Subscription(Base):
    """订阅：一个待监控的阿里云盘分享目录。"""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    share_url: Mapped[str] = mapped_column(Text, default="")
    share_id: Mapped[str] = mapped_column(String(255), default="")
    target_folder_id: Mapped[str] = mapped_column(String(255), default="")
    target_folder_path: Mapped[str] = mapped_column(String(512), default="")
    target_drive_type: Mapped[str] = mapped_column(String(32), default="")
    interval: Mapped[str] = mapped_column(String(64), default="3600")
    naming_template: Mapped[str] = mapped_column(String(255), default="")
    naming_regex: Mapped[str] = mapped_column(String(255), default="")
    # 重命名规则：none(无) / template(命名模板) / prefix_suffix(前缀后缀) / timestamp(时间戳)
    rename_mode: Mapped[str] = mapped_column(String(32), default="none")
    rename_prefix: Mapped[str] = mapped_column(String(128), default="")
    rename_suffix: Mapped[str] = mapped_column(String(128), default="")
    # active | completed | pending_update
    status: Mapped[str] = mapped_column(String(32), default="active")
    # 订阅来源：manual（手动创建）/ tg_monitor（Telegram 频道监控，历史曾用于自动建订阅）
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    share_expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 最后成功转存时间（naive UTC，空则回退 created_at 算超时）；供 SubStatus 完结判定。
    last_transfer_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 链接探测失败累计次数（达阈值 → 链接失效 pending_update）；默认 0。
    link_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )
    remark: Mapped[str] = mapped_column(Text, default="")

    records: Mapped[list["TransferRecord"]] = relationship(
        "TransferRecord",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )
    tasks: Mapped[list["TransferTask"]] = relationship(
        "TransferTask",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )
    runs: Mapped[list["Run"]] = relationship(
        "Run",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )


class TransferRecord(Base):
    """单次转存记录：成功 / 失败 / 跳过。"""

    __tablename__ = "transfer_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), index=True
    )
    source_file_id: Mapped[str] = mapped_column(String(255), default="")
    source_name: Mapped[str] = mapped_column(Text, default="")
    target_file_id: Mapped[str] = mapped_column(String(255), default="")
    target_name: Mapped[str] = mapped_column(Text, default="")
    # success | failed | skipped
    status: Mapped[str] = mapped_column(String(32), default="success")
    message: Mapped[str] = mapped_column(Text, default="")
    renamed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    subscription: Mapped["Subscription"] = relationship(
        "Subscription", back_populates="records"
    )


class TransferTask(Base):
    """单文件粒度转存任务（PRD §3.1，T-D1）。

    状态机：
        pending → running → {success, failed, skipped}
    ``running`` 仅短暂存在（``claim_pending`` 时设置），失败/成功后立即转终态。

    ``next_retry_at`` 字段为「下次可重试时间」，当前调度器不做独立重试线程，
    但保留字段以便 P2 错峰重试使用。
    """

    __tablename__ = "transfer_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id"), nullable=True
    )
    source_file_id: Mapped[str] = mapped_column(String(255), default="")
    source_name: Mapped[str] = mapped_column(Text, default="")
    target_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_name: Mapped[str] = mapped_column(Text, default="")
    # pending | running | success | failed | skipped
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )

    subscription: Mapped["Subscription"] = relationship(
        "Subscription", back_populates="tasks"
    )
    run: Mapped["Run | None"] = relationship("Run", back_populates="tasks")

    __table_args__ = (
        Index("ix_transfer_tasks_sub_status", "subscription_id", "status"),
        Index("ix_transfer_tasks_run_id", "run_id"),
    )


class Run(Base):
    """运行级记录（PRD §3.2，T-D1）。

    每次 ``SubscriptionChecker.check()`` 调用产出一条 run；状态：
        running | success | partial | failed | skipped_locked
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # running | success | partial | failed | skipped_locked
    status: Mapped[str] = mapped_column(String(32), default="running")
    # JSON 字符串: {added, updated, skipped, failed, pending}
    summary: Mapped[str] = mapped_column(Text, default="")
    # scheduled | manual
    run_mode: Mapped[str] = mapped_column(String(16), default="scheduled")

    subscription: Mapped["Subscription"] = relationship(
        "Subscription", back_populates="runs"
    )
    tasks: Mapped[list["TransferTask"]] = relationship(
        "TransferTask", back_populates="run"
    )

    __table_args__ = (
        Index("ix_runs_sub_started", "subscription_id", "started_at"),
    )


class Setting(Base):
    """键值配置表（与 pydantic ``Settings`` 区分，避免命名冲突）。"""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Token(Base):
    """单账号 Token 行（全库仅一行，id 固定为 1）。

    轮转后的 refresh_token 持久化回此表（主理人决策 #8）。
    """

    __tablename__ = "token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    drive_id: Mapped[str] = mapped_column(String(255), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TGMonitorState(Base):
    """Telegram 频道监控去重状态（T-TG）。

    持久化每个被监控频道的「已处理到最后一条消息 id」与「已处理过的分享链接集合」，
    使服务重启后不重复建订阅 / 不重复触发转存。
    """

    __tablename__ = "tg_monitor_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    # 已处理过的分享链接 JSON 数组字符串，便于跨重启去重。
    processed_links: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )


class TGMonitorLog(Base):
    """Telegram 频道监控运行日志（T-TG 日志模块）。

    记录频道关键活动：消息接收时间、频道来源、消息类型、处理状态等，
    用于问题排查。best-effort 写入，不参与订阅变更事务。
    """

    __tablename__ = "tg_monitor_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(255), default="", index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), default="")
    message_type: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    link: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    # 频道推送的具体内容原文（消息正文），供「频道日记」时间轴展示。
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, index=True
    )


# 保持现有 __all__ 兼容性 —— 让旧的 ``from models import *`` 仍然能用。
# 新增模型只需添加到这里。

# 同步管理模块 ORM 实体（移植自 TaoSync 的 engine/mount/job 体系）。
# 在此导入以注册到 Base.metadata，供 init_db 建表。
from models_sync import (  # noqa: F401,E402
    SyncEngine,
    SyncStorageMount,
    SyncJob,
    SyncTask,
    SyncTaskItem,
    SyncSourceSnapshot,
    SyncSourceSnapshotMeta,
    SyncMoveLog,
)

