"""同步管理服务类（SyncService）：封装作业编排层，接入 Flask 服务容器。

把 ``core.sync.job_service`` 的模块级函数按「持有会话工厂 + 通知器」的方式
包装成有状态服务，便于蓝图直接取用。模块级函数保留作为无状态入口，
供测试或后台调度调用；``SyncService`` 仅做依赖注入与边界封装。
"""
from __future__ import annotations

import logging

from core.sync import job_service

logger = logging.getLogger(__name__)


class SyncService:
    """同步管理服务：持有 session_factory 与上层 services（用于通知）。"""

    def __init__(self, session_factory, services=None):
        self.session_factory = session_factory
        self.services = services

    # ---- 启动 ----
    def init_jobs(self) -> None:
        """启动时修正异常任务状态并重建启用作业的调度。"""
        with self.session_factory() as session:
            job_service.init_jobs(session, self.services)

    # ---- 作业 CRUD 编排 ----
    def add_job(self, job: dict):
        with self.session_factory() as session:
            result = job_service.add_job_client(job, session, services=self.services)
            session.commit()
            return result

    def edit_job(self, job: dict):
        with self.session_factory() as session:
            result = job_service.edit_job_client(job, session, services=self.services)
            session.commit()
            return result

    def remove_job(self, job_id: int):
        with self.session_factory() as session:
            job_service.remove_job_client(job_id, session)
            session.commit()

    # ---- 手动触发 / 启停 ----
    def do_job_manual(self, job_id: int, operator="手动"):
        # 仅用于取 / 建 JobClient（缓存命中时只读 identity map）；实际执行在
        # JobClient.do_manual 启动的后台线程内自行开 session 并 commit，不在此长事务中。
        with self.session_factory() as session:
            job_service.do_job_manual(job_id, session, services=self.services, operator=operator)

    def abort_job(self, job_id: int):
        with self.session_factory() as session:
            job_service.abort_job(job_id, session, services=self.services)
            session.commit()

    def pause_job(self, job_id: int):
        with self.session_factory() as session:
            job_service.pause_job(job_id, session, services=self.services)
            session.commit()

    def continue_job(self, job_id: int):
        with self.session_factory() as session:
            job_service.continue_job(job_id, session, services=self.services)
            session.commit()

    def do_all_manual(self):
        with self.session_factory() as session:
            job_service.do_all_job_manual(session, services=self.services)

    def pause_all(self):
        with self.session_factory() as session:
            job_service.pause_all_job(session)
            session.commit()

    def continue_all(self):
        with self.session_factory() as session:
            job_service.continue_all_job(session)
            session.commit()

    # ---- 查询 ----
    def get_job_list_view(self, req=None):
        with self.session_factory() as session:
            return job_service.get_job_list_view(session, req)

    def get_job_current(self, job_id: int, status=None):
        with self.session_factory() as session:
            return job_service.get_job_current(job_id, session, status, self.services)

    # ---- 同步记录（历史日志）编排 ----
    def add_sync_record(self, record: dict) -> int:
        with self.session_factory() as session:
            from core.sync.job_dao import add_sync_record as _add
            rid = _add(session, record)
            session.commit()
            return rid

    def get_sync_record_list(self, params: dict) -> dict:
        with self.session_factory() as session:
            from core.sync.job_dao import get_sync_record_list as _list
            return _list(session, params)

    def get_all_sync_records(self, params: dict = None) -> list:
        with self.session_factory() as session:
            from core.sync.job_dao import get_all_sync_records as _all
            return _all(session, params)

    # ---- 存储目录（挂载）编排：委托 storage_engine ----
    def get_system_engine_id(self):
        from core.sync import engine as storage_engine

        with self.session_factory() as session:
            return storage_engine.get_system_engine_id(session)

    def get_mount_list(self):
        from core.sync import engine as storage_engine

        with self.session_factory() as session:
            engine_id = storage_engine.get_system_engine_id(session)
            return storage_engine.get_mount_list(session, engine_id)

    def add_mount(self, data: dict):
        from core.sync import engine as storage_engine

        with self.session_factory() as session:
            result = storage_engine.add_mount(session, data)
            session.commit()
            return result

    def update_mount(self, mount_id: int, data: dict):
        from core.sync import engine as storage_engine

        with self.session_factory() as session:
            result = storage_engine.update_mount(session, mount_id, data)
            session.commit()
            return result

    def remove_mount(self, mount_id: int):
        from core.sync import engine as storage_engine

        with self.session_factory() as session:
            storage_engine.remove_mount(session, mount_id)
            session.commit()

    def get_supported_drivers(self):
        from core.sync import engine as storage_engine

        return storage_engine.get_supported_drivers()
