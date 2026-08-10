"""同步服务聚合（对应 TaoSync controller 层 + 本项目 Services 容器接入点）。

``SyncService`` 把同步引擎 / 存储目录 / 作业 / 任务 / 记录各子模块组合起来，
向上对蓝图暴露统一 API；向下注入 ``session_factory`` 和 ``notifier``
（复用当前 Telegram 通知）。调度由每个 ``JobClient`` 自带的 BackgroundScheduler
驱动（与 TG 监控线程同模式，不依赖外部调度器）。
"""
from __future__ import annotations

import logging

from core.sync import job_dao, job_service, task_service
from core.sync_storage.engine import (
    AlistClient,
    StorageMountDAO,
    StorageService,
    SyncEngineDAO,
)

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, session_factory, notifier=None):
        self._sf = session_factory
        self._notifier = notifier
        self._engine_dao = SyncEngineDAO(session_factory)
        self._mount_dao = StorageMountDAO(session_factory)
        self._storage_service = None  # 懒加载（依赖 job_dao 等，避免循环）

    # ───────────────── 同步引擎（alist 表） ─────────────────
    def get_engine_list(self):
        clients = self._engine_dao.get_engine_list()
        for client in clients:
            client.pop('token', None)
            if client.get('engineType') == 'taosync':
                client['displayName'] = 'TaoSync（内置）'
                client['directoryCount'] = len(self._mount_dao.get_mount_list(client['id']))
            else:
                client['displayName'] = client.get('remark') or client.get('url')
        return clients

    def add_engine(self, engine):
        # 外部 AList：先验证连通性（会抛异常）。
        if engine.get('engineType') != 'taosync':
            url = engine.get('url', '')
            if url.endswith('/'):
                url = url[:-1]
            client = AlistClient(url, engine.get('token') or '')
            engine_id = self._engine_dao.add_engine({
                'remark': engine.get('remark'),
                'url': url,
                'userName': client.user,
                'token': engine.get('token'),
                'engineType': 'alist',
                'systemKey': 'alist',
                'protected': 0,
            })
            client.updateAlistId(engine_id)
        else:
            engine_id = self._engine_dao.add_engine({
                'remark': engine.get('remark'),
                'url': '',
                'userName': 'TaoSync',
                'token': '',
                'engineType': 'taosync',
                'systemKey': 'taosync',
                'protected': 0,
            })
        return engine_id

    def update_engine(self, engine):
        self._engine_dao.update_engine(engine)

    def remove_engine(self, engine_id):
        self._engine_dao.remove_engine(engine_id)

    # ───────────────── 存储目录（storage_mount） ─────────────────
    def _storage(self):
        if self._storage_service is None:
            self._storage_service = StorageService(self._sf)
        return self._storage_service

    def get_mount_list(self, engine_id):
        return self._storage().get_mount_list(engine_id)

    def get_supported_drivers(self):
        return self._storage().get_supported_drivers()

    def add_mount(self, data):
        return self._storage().add_mount(data)

    def update_mount(self, data):
        self._storage().update_mount(data)

    def remove_mount(self, mount_id):
        self._storage().remove_mount(mount_id)

    # ───────────────── 作业 job ─────────────────
    def init_jobs(self):
        job_service.init_jobs(self._sf, self._notifier)

    def get_job_list(self, req=None):
        return job_service.get_job_list_view(req or {}, self._sf)

    def get_job_by_id(self, job_id):
        return job_dao.get_job_by_id(job_id, self._sf)

    def add_job(self, job):
        return job_service.add_job_client(job, False, self._sf, self._notifier)

    def update_job(self, job):
        job_service.edit_job_client(job, self._sf, self._notifier)

    def remove_job(self, job_id):
        job_service.remove_job_client(job_id, self._sf, self._notifier)

    def do_job_manual(self, job_id):
        job_service.do_job_manual(job_id, self._sf, self._notifier)

    def do_all_job_manual(self):
        job_service.do_all_job_manual(self._sf, self._notifier)

    def pause_job(self, job_id):
        job_service.pause_job(job_id, self._sf, self._notifier)

    def continue_job(self, job_id):
        job_service.continue_job(job_id, self._sf, self._notifier)

    def pause_all_job(self):
        job_service.pause_all_job(self._sf, self._notifier)

    def continue_all_job(self):
        job_service.continue_all_job(self._sf, self._notifier)

    def abort_job(self, job_id):
        job_service.abort_job(job_id, self._sf, self._notifier)

    def get_job_current(self, job_id, status=None):
        return job_service.get_job_current(job_id, status, self._sf, self._notifier)

    def validate_mounts_exist(self):
        return job_service.validate_mounts_exist(self._sf)

    # ───────────────── 任务 task ─────────────────
    def get_task_list(self, req):
        return task_service.get_task_list(req, self._sf)

    def get_task_item_list(self, req):
        return task_service.get_task_item_list(req, self._sf)

    def remove_task(self, task_id):
        task_service.remove_task(task_id, self._sf)

    # ───────────────── 运行记录 sync_records ─────────────────
    def get_record_list(self, job_id=None, page_size=None, page_num=None):
        return job_dao.get_sync_record_list(
            job_id, page_size=page_size, page_num=page_num, session_factory=self._sf)
