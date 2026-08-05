"""转存任务 / 运行仓储（T-D1 / T-D3 / T-D5）。

封装 ``transfer_tasks`` / ``runs`` 表的 CRUD；所有持久化细节收敛在此，业务层
（``SubscriptionChecker`` / ``SchedulerService`` / 详情页路由）按方法调用。

设计原则：
- 单文件单函数，单 SQL 完成；事务粒度由调用方控制（``session_factory()``）；
- ``claim_pending`` 用 ``UPDATE ... WHERE status='pending' AND attempts<max`` 实现
  原子领取，避免两个协程同时跑同一文件；
- ``finish_run`` 同步写 ``summary`` JSON 与 ``finished_at``，UI 直接读；
- 公开方法**全部**接 ``session`` 参数，调用方负责 session 生命周期（便于业务层在
  一次事务里完成「领任务 → 转存 → 收尾」）。

注意：本期 ``Run`` 与 ``TransferTask`` 与老 ``TransferRecord`` 并存；不修改老表。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from db import utc_now
from models import Run, TransferTask

logger = logging.getLogger(__name__)


# ====================== 任务状态 ======================
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"

# ====================== 运行状态 ======================
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_LOCKED = "skipped_locked"


class TransferRepo:
    """任务 / 运行仓储。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    # ====================== Run ======================
    def start_run(
        self,
        session: Session,
        subscription_id: int,
        run_mode: str = "scheduled",
    ) -> Run:
        """新建一个 Run 记录（status='running'）并返回 ORM 对象。

        内部 ``session.flush()`` 取 id；事务提交由调用方负责（业务方往往要在
        同一个事务里先建 run、再建 task、再 commit）。
        """
        run = Run(
            subscription_id=subscription_id,
            started_at=utc_now(),
            status=RUN_RUNNING,
            run_mode=run_mode,
            summary="{}",
        )
        session.add(run)
        session.flush()
        return run

    def finish_run(
        self,
        session: Session,
        run_id: int,
        status: str,
        summary: Optional[dict[str, Any]] = None,
        commit: bool = False,
    ) -> Optional[Run]:
        """收尾一个 run：写入 status / finished_at / summary。

        ``commit=False`` 时仅 flush（保持原事务粒度）；测试/独立调用可传 ``True``。
        """
        run = session.get(Run, run_id)
        if run is None:
            return None
        run.status = status
        run.finished_at = utc_now()
        if summary is not None:
            run.summary = json.dumps(summary, ensure_ascii=False)
        if commit:
            session.commit()
        else:
            session.flush()
        return run

    def list_runs(
        self,
        session: Session,
        subscription_id: int,
        limit: int = 20,
    ) -> list[Run]:
        """按 started_at desc 列出最近 N 条 run。"""
        return (
            session.query(Run)
            .filter(Run.subscription_id == subscription_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
            .all()
        )

    def get_run(self, session: Session, run_id: int) -> Optional[Run]:
        return session.get(Run, run_id)

    # ====================== Task ======================
    def create_task(
        self,
        session: Session,
        subscription_id: int,
        source_file_id: str,
        source_name: str,
        target_name: str,
        run_id: Optional[int] = None,
        status: str = TASK_PENDING,
        commit: bool = False,
    ) -> TransferTask:
        """新建一条任务记录。默认 status='pending'。"""
        task = TransferTask(
            subscription_id=subscription_id,
            run_id=run_id,
            source_file_id=source_file_id,
            source_name=source_name,
            target_name=target_name,
            status=status,
            attempts=0,
        )
        session.add(task)
        if commit:
            session.commit()
        else:
            session.flush()
        return task

    def claim_pending(
        self,
        session: Session,
        task_id: int,
        max_attempts: int = 3,
        commit: bool = False,
    ) -> Optional[TransferTask]:
        """原子领取一个 pending 任务。

        同一 SQL 完成：仅当 ``status='pending' AND attempts<max_attempts`` 时
        把状态置为 ``running`` 并 ``attempts += 1``。否则返回 ``None``。
        兼容 SQLite + 主流 DB（UPDATE ... WHERE）。
        """
        result = session.execute(
            __import__("sqlalchemy").text(
                "UPDATE transfer_tasks "
                "SET status='running', attempts=attempts+1, updated_at=:now "
                "WHERE id=:tid AND status='pending' AND attempts<:max"
            ),
            {"now": utc_now(), "tid": task_id, "max": max_attempts},
        )
        if result.rowcount != 1:
            return None
        if commit:
            session.commit()
        else:
            session.flush()
        return session.get(TransferTask, task_id)

    def finish_task(
        self,
        session: Session,
        task_id: int,
        status: str,
        *,
        target_file_id: Optional[str] = None,
        target_name: Optional[str] = None,
        last_error: Optional[str] = None,
        error_kind: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[TransferTask]:
        """收尾一条 task：写终态 + 关联字段。"""
        task = session.get(TransferTask, task_id)
        if task is None:
            return None
        task.status = status
        if target_file_id is not None:
            task.target_file_id = target_file_id
        if target_name is not None:
            task.target_name = target_name
        if last_error is not None:
            task.last_error = last_error
        if error_kind is not None:
            task.error_kind = error_kind
        if next_retry_at is not None:
            task.next_retry_at = next_retry_at
        if commit:
            session.commit()
        else:
            session.flush()
        return task

    def list_tasks(
        self,
        session: Session,
        subscription_id: int,
        limit: int = 50,
    ) -> list[TransferTask]:
        """按 updated_at desc 列出最近 N 条 task。"""
        return (
            session.query(TransferTask)
            .filter(TransferTask.subscription_id == subscription_id)
            .order_by(TransferTask.updated_at.desc())
            .limit(limit)
            .all()
        )

    def list_pending_for_sub(
        self,
        session: Session,
        subscription_id: int,
    ) -> list[TransferTask]:
        """列出某订阅下 status='pending' 的 task（启动续跑用）。"""
        return (
            session.query(TransferTask)
            .filter(
                TransferTask.subscription_id == subscription_id,
                TransferTask.status == TASK_PENDING,
            )
            .order_by(TransferTask.next_retry_at.is_(None), TransferTask.next_retry_at)
            .all()
        )

    def list_all_pending(
        self,
        session: Session,
        max_attempts: int = 3,
    ) -> list[TransferTask]:
        """列出全部 active 订阅的 pending task（启动续跑用）。

        ``max_attempts`` 用于过滤已用尽重试次数的 task。
        """
        from models import Subscription

        return (
            session.query(TransferTask)
            .join(Subscription, Subscription.id == TransferTask.subscription_id)
            .filter(
                TransferTask.status == TASK_PENDING,
                TransferTask.attempts < max_attempts,
                Subscription.status == "active",
            )
            .order_by(TransferTask.next_retry_at.is_(None), TransferTask.next_retry_at)
            .all()
        )
