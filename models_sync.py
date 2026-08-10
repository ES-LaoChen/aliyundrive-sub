"""同步管理模块 ORM 实体（移植自 TaoSync 的 engine/mount/job 体系）。

在此定义并注册到 ``db.Base.metadata``，供 ``init_db`` 建表。字段命名沿用 TaoSync
的 ``mapper`` 原始列名（camelCase），以降低 SQL→ORM 的翻译成本。

表清单：
- ``alist``：同步引擎（内置 taosync + 外部 OpenList/AList 实例）。
- ``storage_mount``：某引擎下的虚拟存储目录（挂载），后端为 local。
- ``job``：同步作业。
- ``job_task``：作业的一次运行（任务）。
- ``job_task_item``：运行中的单文件条目。
- ``job_source_snapshot`` / ``job_source_snapshot_meta``：源目录快照（增量同步）。
- ``sync_records``：同步运行记录（审计 / 导出 / 过滤）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db import Base, utc_now


class SyncEngine(Base):
    """同步引擎：内置 ``taosync`` 或外部 OpenList/AList 实例。"""

    __tablename__ = "alist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    userName: Mapped[str] = mapped_column(String(255), default="")
    token: Mapped[str] = mapped_column(Text, default="")
    engineType: Mapped[str] = mapped_column(String(32), default="taosync")
    systemKey: Mapped[str] = mapped_column(String(64), default="taosync")
    protected: Mapped[int] = mapped_column(Integer, default=0)
    createTime: Mapped[int] = mapped_column(Integer, default=0)


class SyncStorageMount(Base):
    """存储目录（挂载）：绑定到某引擎，后端为 local。"""

    __tablename__ = "storage_mount"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engineId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    driverType: Mapped[str] = mapped_column(String(32), default="local")
    config: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    authVersion: Mapped[int] = mapped_column(Integer, default=1)
    configVersion: Mapped[int] = mapped_column(Integer, default=1)
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_storage_mount_engine", "engineId", "name"),)


class SyncJob(Base):
    """同步作业。"""

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enable: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str] = mapped_column(Text, default="")
    srcPath: Mapped[str] = mapped_column(Text, default="")
    dstPath: Mapped[str] = mapped_column(Text, default="")
    alistId: Mapped[int] = mapped_column(Integer, default=0)
    useCacheT: Mapped[int] = mapped_column(Integer, default=1)
    scanIntervalT: Mapped[int] = mapped_column(Integer, default=0)
    useCacheS: Mapped[int] = mapped_column(Integer, default=1)
    scanIntervalS: Mapped[int] = mapped_column(Integer, default=0)
    # 同步模式：0-增量(add only) 1-全量(full) 2-移动(move)
    method: Mapped[int] = mapped_column(Integer, default=0)
    sourceMode: Mapped[int] = mapped_column(Integer, default=0)
    interval: Mapped[int] = mapped_column(Integer, default=0)
    isCron: Mapped[int] = mapped_column(Integer, default=0)
    year: Mapped[str] = mapped_column(String(16), default="")
    month: Mapped[str] = mapped_column(String(16), default="")
    day: Mapped[str] = mapped_column(String(16), default="")
    week: Mapped[str] = mapped_column(String(16), default="")
    day_of_week: Mapped[str] = mapped_column(String(16), default="")
    hour: Mapped[str] = mapped_column(String(16), default="")
    minute: Mapped[str] = mapped_column(String(16), default="")
    second: Mapped[str] = mapped_column(String(16), default="")
    start_date: Mapped[str] = mapped_column(String(32), default="")
    end_date: Mapped[str] = mapped_column(String(32), default="")
    exclude: Mapped[str] = mapped_column(Text, default="")
    minFileSize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maxFileSize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    # snake_case 兼容别名（移植层与 ORM 字段混用时的坑；保留以对齐旧调用）。
    @property
    def is_cron(self) -> int:
        return self.isCron

    @property
    def src_path(self) -> str:
        return self.srcPath

    @property
    def dst_path(self) -> str:
        return self.dstPath

    @property
    def source_mode(self) -> int:
        return self.sourceMode


class SyncTask(Base):
    """作业的一次运行（任务）。"""

    __tablename__ = "job_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    runTime: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int] = mapped_column(Integer, default=0)
    errMsg: Mapped[str] = mapped_column(Text, default="")
    taskNum: Mapped[str] = mapped_column(Text, default="")
    createTime: Mapped[int] = mapped_column(Integer, default=0)


class SyncTaskItem(Base):
    """运行中的单文件条目。"""

    __tablename__ = "job_task_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taskId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    srcPath: Mapped[str] = mapped_column(Text, default="")
    dstPath: Mapped[str] = mapped_column(Text, default="")
    isPath: Mapped[int] = mapped_column(Integer, default=0)
    fileName: Mapped[str] = mapped_column(Text, default="")
    fileSize: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[int] = mapped_column(Integer, default=0)
    alistTaskId: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    errMsg: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[float] = mapped_column(Integer, default=0)
    createTime: Mapped[int] = mapped_column(Integer, default=0)


class SyncSourceSnapshot(Base):
    """源目录快照条目（增量同步）。"""

    __tablename__ = "job_source_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    isDir: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncSourceSnapshotMeta(Base):
    """源目录快照元信息。"""

    __tablename__ = "job_source_snapshot_meta"

    jobId: Mapped[int] = mapped_column(Integer, primary_key=True)
    initialized: Mapped[int] = mapped_column(Integer, default=0)
    scanTime: Mapped[int] = mapped_column(Integer, default=0)
    entryCount: Mapped[int] = mapped_column(Integer, default=0)


class SyncMoveLog(Base):
    """移动模式已成功移动的源文件记录（防重复移动）。"""

    __tablename__ = "sync_move_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    taskId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    jobId: Mapped[int] = mapped_column(Integer, default=0, index=True)


class SyncRecord(Base):
    """同步运行记录（审计 / 导出 / 过滤）。"""

    __tablename__ = "sync_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(Integer, default=0, index=True)
    jobName: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    dataCount: Mapped[int] = mapped_column(Integer, default=0)
    dataSize: Mapped[int] = mapped_column(Integer, default=0)
    errMsg: Mapped[str] = mapped_column(Text, default="")
    startTime: Mapped[int] = mapped_column(Integer, default=0)
    endTime: Mapped[int] = mapped_column(Integer, default=0)
    createTime: Mapped[int] = mapped_column(Integer, default=0)
