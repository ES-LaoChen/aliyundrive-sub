"""同步管理模块 ORM 实体（移植自 TaoSync 的 engine/mount/job 体系）。

设计说明（与现有项目风格一致）：
- 不再使用 TaoSync 的 ``sqlite3`` + ``sqlBase`` 手写脚本，全部改为 SQLAlchemy ORM，
  由 ``db.init_db`` 的 ``Base.metadata.create_all`` 自动建表。
- 表前缀 ``sync_`` 避免与现有 ``subscriptions`` / ``transfer_*`` / ``runs`` 等冲突。
- 字段命名与 TaoSync 原 ``job`` / ``storage_mount`` / ``job_task`` / ``job_task_item`` /
  ``job_source_snapshot`` 保持一致，便于移植算法层直接读写。

移植范围（按用户决策）：
- 引擎（engine）：仅内置 ``taosync`` 系统引擎，外加可选的外部 AList（沿用 TaoSync 概念但本项目
  默认只有内置引擎；外部 AList 留作扩展，UI 不强制）。
- 存储挂载（storage_mount）：local / smb / ftp / sftp / aliyun 五种 driver。
- 同步作业（sync_job）：仅「移动模式」(method=2)，调度支持 interval / cron / 手动。
- 任务（sync_task）/ 任务明细（sync_task_item）/ 源快照（sync_source_snapshot）。
"""
from __future__ import annotations

import json
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base, utc_now


# ============================================================
# 引擎（engine）：沿用 TaoSync 的 alist_list 概念，内置 taosync 系统引擎。
# 本项目默认只有一个受保护的内置引擎（id=1, systemKey='taosync'）。
# ============================================================
class SyncEngine(Base):
    """存储引擎：内置 TaoSync 引擎 + 可选外部 AList。"""

    __tablename__ = "sync_engines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    # TaoSync 内置引擎的 url/userName/token 留空；外部 AList 才填。
    url: Mapped[str] = mapped_column(Text, default="")
    userName: Mapped[str] = mapped_column(String(255), default="")
    token: Mapped[str] = mapped_column(Text, default="")
    # engineType: 'taosync' | 'alist'；systemKey: 内置系统引擎固定 'taosync'。
    engineType: Mapped[str] = mapped_column(String(32), default="taosync")
    systemKey: Mapped[str] = mapped_column(String(32), default="")
    # protected: 内置系统引擎不可删除（1=受保护）。
    protected: Mapped[int] = mapped_column(Integer, default=0)
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    mounts: Mapped[list["SyncStorageMount"]] = relationship(
        "SyncStorageMount",
        back_populates="engine",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["SyncJob"]] = relationship(
        "SyncJob",
        back_populates="engine",
        cascade="all, delete-orphan",
    )


# ============================================================
# 存储挂载（storage_mount）：虚拟目录，对应一个 driver 后端。
# ============================================================
class SyncStorageMount(Base):
    """存储挂载点：一个虚拟目录 → 一种 driver（local/smb/ftp/sftp/aliyun）。"""

    __tablename__ = "sync_storage_mount"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engineId: Mapped[int] = mapped_column(ForeignKey("sync_engines.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    driverType: Mapped[str] = mapped_column(String(32), default="local")
    # driver 配置 JSON（含凭证，密码等 secret 字段在对外序列化时遮蔽）。
    config: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    # authVersion / configVersion：用于云端 token 轮换的乐观锁（与 TaoSync 一致）。
    authVersion: Mapped[int] = mapped_column(Integer, default=1)
    configVersion: Mapped[int] = mapped_column(Integer, default=1)
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    engine: Mapped["SyncEngine"] = relationship(
        "SyncEngine", back_populates="mounts"
    )

    __table_args__ = (
        Index("ix_sync_mount_engine_enabled", "engineId", "enabled"),
    )

    def config_dict(self) -> dict:
        try:
            return json.loads(self.config or "{}")
        except (TypeError, ValueError):
            return {}


# ============================================================
# 同步作业（job）：仅移动模式（method=2）。
# ============================================================
class SyncJob(Base):
    """同步作业：源路径 → 多个目标路径，移动模式。"""

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    engineId: Mapped[int] = mapped_column(
        ForeignKey("sync_engines.id"), index=True
    )
    enable: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str] = mapped_column(String(255), default="")
    srcPath: Mapped[str] = mapped_column(Text, default="")
    # 多个目标以 ':' 分隔（与 TaoSync 一致）。
    dstPath: Mapped[str] = mapped_column(Text, default="")
    # 调度参数（interval / cron）。
    useCacheT: Mapped[int] = mapped_column(Integer, default=0)
    scanIntervalT: Mapped[int] = mapped_column(Integer, default=0)
    useCacheS: Mapped[int] = mapped_column(Integer, default=0)
    scanIntervalS: Mapped[int] = mapped_column(Integer, default=0)
    # method: 0-仅新增 1-全量 2-移动（本项目仅启用 2）。
    method: Mapped[int] = mapped_column(Integer, default=2)
    # sourceMode: 0-常规 1-源目录模式（仅扫源比对快照）。
    sourceMode: Mapped[int] = mapped_column(Integer, default=0)
    interval: Mapped[int] = mapped_column(Integer, default=60)
    # isCron: 0-interval 1-cron 2-手动（仅手动触发）。
    isCron: Mapped[int] = mapped_column(Integer, default=0)
    # cron 字段（year/month/day/week/day_of_week/hour/minute/second/start_date/end_date）。
    year: Mapped[str] = mapped_column(String(64), default="")
    month: Mapped[str] = mapped_column(String(64), default="")
    day: Mapped[str] = mapped_column(String(64), default="")
    week: Mapped[str] = mapped_column(String(64), default="")
    day_of_week: Mapped[str] = mapped_column(String(64), default="")
    hour: Mapped[str] = mapped_column(String(64), default="")
    minute: Mapped[str] = mapped_column(String(64), default="")
    second: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[str] = mapped_column(String(64), default="")
    end_date: Mapped[str] = mapped_column(String(64), default="")
    # 排除规则（gitignore 风格，':' 分隔）。
    exclude: Mapped[str] = mapped_column(Text, default="")
    minFileSize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maxFileSize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    engine: Mapped["SyncEngine"] = relationship(
        "SyncEngine", back_populates="jobs"
    )
    tasks: Mapped[list["SyncTask"]] = relationship(
        "SyncTask", back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_sync_job_engine", "engineId"),
    )

    # snake_case 别名：兼容移植自 TaoSync 的同步引擎（job_client 使用 snake_case 字段）。
    @property
    def is_cron(self):
        return self.isCron

    @property
    def src_path(self):
        return self.srcPath

    @property
    def dst_path(self):
        return self.dstPath

    @property
    def source_mode(self):
        return self.sourceMode

    @property
    def min_file_size(self):
        return self.minFileSize

    @property
    def max_file_size(self):
        return self.maxFileSize

    @property
    def scan_interval_t(self):
        return self.scanIntervalT

    @property
    def scan_interval_s(self):
        return self.scanIntervalS

    @property
    def use_cache_t(self):
        return self.useCacheT

    @property
    def use_cache_s(self):
        return self.useCacheS


# ============================================================
# 同步任务（task）：一次作业执行的运行记录。
# ============================================================
class SyncTask(Base):
    """同步任务：每次作业执行产生一条 task。"""

    __tablename__ = "sync_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id"), index=True
    )
    runTime: Mapped[int] = mapped_column(Integer, default=0)
    # status: 0-等待 1-进行中 2-成功 3-部分失败 4-中止 5-超时 6-失败 7-其他。
    status: Mapped[int] = mapped_column(Integer, default=0)
    errMsg: Mapped[str] = mapped_column(Text, default="")
    # taskNum: 执行结果统计 JSON（与 TaoSync 一致）。
    taskNum: Mapped[str] = mapped_column(Text, default="")

    job: Mapped["SyncJob"] = relationship("SyncJob", back_populates="tasks")
    items: Mapped[list["SyncTaskItem"]] = relationship(
        "SyncTaskItem", back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_sync_task_job", "jobId"),
    )


# ============================================================
# 同步任务明细（task_item）：每个文件的复制/删除动作。
# ============================================================
class SyncTaskItem(Base):
    """同步任务明细：单文件/单目录的复制或删除动作。"""

    __tablename__ = "sync_task_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taskId: Mapped[int] = mapped_column(
        ForeignKey("sync_tasks.id"), index=True
    )
    srcPath: Mapped[str] = mapped_column(Text, default="")
    dstPath: Mapped[str] = mapped_column(Text, default="")
    # isPath: 0-文件 1-目录。
    isPath: Mapped[int] = mapped_column(Integer, default=0)
    fileName: Mapped[str] = mapped_column(Text, default="")
    fileSize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # type: 0-复制 1-删除 2-移动。
    type: Mapped[int] = mapped_column(Integer, default=0)
    # alistTaskId：内部 copy 任务 id（本项目用于移动模式追踪）。
    alistTaskId: Mapped[str] = mapped_column(String(64), default="")
    # status: 0-等待 1-进行中 2-成功 3-取消中 4-已取消 5-出错重试 6-失败中 7-失败 …。
    status: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Integer, default=0)  # 0-100
    errMsg: Mapped[str] = mapped_column(Text, default="")
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    task: Mapped["SyncTask"] = relationship("SyncTask", back_populates="items")

    __table_args__ = (
        Index("ix_sync_item_task", "taskId"),
    )


# ============================================================
# 源快照（source_snapshot）：源目录模式下的完整文件视图。
# ============================================================
class SyncSourceSnapshot(Base):
    """源快照：sourceMode=1 时存储源目录完整文件视图。"""

    __tablename__ = "sync_source_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id"), index=True
    )
    path: Mapped[str] = mapped_column(Text, default="")
    isDir: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_sync_snapshot_job", "jobId", "path"),
    )


class SyncSourceSnapshotMeta(Base):
    """源快照元信息：initialized / scanTime / entryCount。"""

    __tablename__ = "sync_source_snapshot_meta"

    jobId: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id"), primary_key=True
    )
    initialized: Mapped[int] = mapped_column(Integer, default=0)
    scanTime: Mapped[int] = mapped_column(Integer, default=0)
    entryCount: Mapped[int] = mapped_column(Integer, default=0)


# ============================================================
# 移动日志（move_log）：移动模式专属，记录已成功移动的文件名（避免重复移动）。
# ============================================================
class SyncMoveLog(Base):
    """移动日志：移动模式逐次成功删除源文件后追加一条记录。"""

    __tablename__ = "sync_move_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jobId: Mapped[int] = mapped_column(
        ForeignKey("sync_jobs.id"), index=True
    )
    fileName: Mapped[str] = mapped_column(Text, default="")
    srcPath: Mapped[str] = mapped_column(Text, default="")
    createTime: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_sync_movelog_job", "jobId"),
    )


def sync_source_snapshot_identity(job) -> dict:
    """从 job 提取源快照身份（与 TaoSync jobMapper.sourceSnapshotIdentity 一致）。"""
    fields = (
        "engineId", "srcPath", "dstPath", "method", "exclude",
        "minFileSize", "maxFileSize",
    )
    identity = {key: getattr(job, key, None) for key in fields}
    for key in ("engineId", "method", "minFileSize", "maxFileSize"):
        value = identity.get(key)
        if value is not None:
            try:
                identity[key] = int(value)
            except (TypeError, ValueError):
                pass
    return identity


SOURCE_SNAPSHOT_FIELDS = (
    "engineId", "srcPath", "dstPath", "method", "exclude",
    "minFileSize", "maxFileSize",
)
