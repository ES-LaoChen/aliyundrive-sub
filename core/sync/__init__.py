"""同步管理模块：作业引擎 + 存储引擎 + 任务/记录。

子模块：
- ``job_dao`` / ``job_client`` / ``job_service`` / ``task_service`` / ``move_log``：作业引擎
- ``service.SyncService``：聚合服务（对接 Services 容器）
"""
from core.sync.service import SyncService

__all__ = ["SyncService"]
