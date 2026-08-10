"""移动模式已移动文件日志（移植自 TaoSync service/syncJob/moveLog.py）。

源实现用 JSONL 文件；本项目统一以 ORM 表 ``sync_move_logs`` 存储，复用会话工厂。
作用不变：移动模式在复制决策前读取本日志，命中已记录文件名则跳过本次移动，
避免重复移动同名文件。
"""
from __future__ import annotations

import time

from db import get_session_local

from models_sync import SyncMoveLog


def load_moved_file_names(job_id, session_factory=None):
    """读取已移动文件名集合（仅文件名，不含目录）。"""
    sf = session_factory or get_session_local()
    with sf() as db:
        rows = db.query(SyncMoveLog).filter_by(jobId=int(job_id)).all()
        return {r.name for r in rows if r.name}


def append_moved_file(job_id, file_name, src_path=None, file_time=None,
                      session_factory=None):
    """追加一条移动成功记录（含文件名）。"""
    if not file_name:
        return
    sf = session_factory or get_session_local()
    with sf() as db:
        db.add(SyncMoveLog(
            name=file_name,
            type=0,
            size=0,
            taskId=0,
            jobId=int(job_id),
        ))
        db.commit()
