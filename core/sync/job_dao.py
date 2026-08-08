"""同步作业 / 任务 / 快照 / 移动日志的 SQLAlchemy DAO。

移植自 TaoSync ``mapper/jobMapper.py``，把原生 SQL 改为 ORM 会话操作，逻辑一致。
源快照乐观锁（job 身份字段比对）保留，避免同步期间 job 被编辑导致数据错位。
"""
from __future__ import annotations

import time

from models_sync import (
    SOURCE_SNAPSHOT_FIELDS,
    SyncJob,
    SyncMoveLog,
    SyncSourceSnapshot,
    SyncSourceSnapshotMeta,
    SyncTask,
    SyncTaskItem,
    sync_source_snapshot_identity,
)


# ---------- 作业（job） ----------

def source_snapshot_identity(job) -> dict:
    if isinstance(job, dict):
        return {k: job.get(k) for k in SOURCE_SNAPSHOT_FIELDS}
    return sync_source_snapshot_identity(job)


def get_job_list(db, params=None):
    if params:
        page = int(params.get("pageNum", 1))
        size = int(params.get("pageSize", 20))
        total = db.query(SyncJob).count()
        rows = (
            db.query(SyncJob)
            .order_by(SyncJob.createTime.desc())
            .limit(size)
            .offset((page - 1) * size)
            .all()
        )
        return {
            "dataList": rows,
            "total": total,
            "pageNum": page,
            "pageSize": size,
        }
    return db.query(SyncJob).order_by(SyncJob.createTime.desc()).all()


def get_latest_job_task_list(db, job_ids: list) -> list:
    """返回每个 job 最新一条 task 及其明细计数。"""
    if not job_ids:
        return []
    results = []
    for job_id in job_ids:
        task = (
            db.query(SyncTask)
            .filter_by(jobId=job_id)
            .order_by(SyncTask.runTime.desc(), SyncTask.id.desc())
            .first()
        )
        if task is None:
            continue
        items = db.query(SyncTaskItem).filter_by(taskId=task.id).all()
        item_all = len(items)
        item_success = sum(1 for i in items if i.status == 2)
        item_fail = sum(1 for i in items if i.status == 7)
        item_other = sum(1 for i in items if i.status not in (0, 1, 2, 7))
        results.append(
            {
                "id": task.id,
                "jobId": task.jobId,
                "status": task.status,
                "errMsg": task.errMsg,
                "runTime": task.runTime,
                "taskNum": task.taskNum,
                "createTime": task.createTime,
                "itemAllNum": item_all,
                "itemSuccessNum": item_success,
                "itemFailNum": item_fail,
                "itemOtherNum": item_other,
            }
        )
    return results


def get_enable_job_list(db):
    return db.query(SyncJob).filter_by(enable=1).all()


def get_job_by_id(db, job_id: int) -> SyncJob:
    row = db.query(SyncJob).filter_by(id=job_id).first()
    if row is None:
        raise ValueError("job not found")
    return row


def get_job_by_task_id(db, task_id: int) -> SyncJob:
    row = (
        db.query(SyncJob)
        .filter(SyncJob.id.in_(
            db.query(SyncTask.jobId).filter_by(id=task_id)
        ))
        .first()
    )
    if row is None:
        raise ValueError("job not found")
    return row


def add_job(db, job: dict) -> int:
    row = SyncJob(**{k: v for k, v in job.items() if k != "id"})
    db.add(row)
    db.flush()
    return row.id


def update_job(db, job: dict, clear_source_snapshot=False) -> None:
    row = get_job_by_id(db, job["id"])
    for key, value in job.items():
        if key == "id":
            continue
        setattr(row, key, value)
    if clear_source_snapshot:
        db.query(SyncSourceSnapshot).filter_by(jobId=row.id).delete()
        db.query(SyncSourceSnapshotMeta).filter_by(jobId=row.id).delete()


def update_job_enable(db, job_id: int, enable: int) -> None:
    db.query(SyncJob).filter_by(id=job_id).update({"enable": enable})


def delete_job(db, job_id: int) -> None:
    db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
    db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).delete()
    db.query(SyncTaskItem).filter(
        SyncTaskItem.taskId.in_(db.query(SyncTask.id).filter_by(jobId=job_id))
    ).delete(synchronize_session=False)
    db.query(SyncTask).filter_by(jobId=job_id).delete()
    db.query(SyncMoveLog).filter_by(jobId=job_id).delete()
    db.query(SyncJob).filter_by(id=job_id).delete()


# ---------- 源快照（source_snapshot） ----------

def get_source_snapshot(db, job_id: int) -> dict:
    meta = db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).first()
    if meta is None:
        meta = {"jobId": int(job_id), "initialized": 0, "scanTime": None, "entryCount": 0}
    else:
        meta = {
            "jobId": meta.jobId,
            "initialized": meta.initialized,
            "scanTime": meta.scanTime,
            "entryCount": meta.entryCount,
        }
    entries = []
    for row in db.query(SyncSourceSnapshot).filter_by(jobId=job_id).order_by(SyncSourceSnapshot.path).all():
        entry = {"path": row.path, "isDir": row.isDir, "size": row.size}
        if row.fingerprint:
            entry["fingerprint"] = row.fingerprint
        entries.append(entry)
    return {"meta": meta, "entries": entries}


def replace_source_snapshot(db, job_id: int, entries: list, expected_identity=None) -> dict:
    rows_by_path = {}
    for entry in entries:
        path = entry["path"]
        if not isinstance(path, str):
            raise ValueError("snapshot path must be a string")
        is_dir = 1 if entry.get("isDir", entry.get("is_dir", 0)) else 0
        fingerprint = entry.get("fingerprint")
        if fingerprint is not None:
            fingerprint = str(fingerprint)
        rows_by_path[path] = (
            int(job_id),
            path,
            is_dir,
            None if is_dir else entry.get("size"),
            fingerprint,
        )
    rows = list(rows_by_path.values())
    scan_time = int(time.time())
    job_row = db.query(SyncJob).filter_by(id=job_id).first()
    if job_row is None:
        raise ValueError("job not found")
    if expected_identity is not None:
        current_identity = source_snapshot_identity(job_row)
        if current_identity != expected_identity:
            raise RuntimeError("job changed during sync")
    db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
    if rows:
        db.bulk_insert_mappings(SyncSourceSnapshot, [
            {
                "jobId": r[0], "path": r[1], "isDir": r[2],
                "size": r[3], "fingerprint": r[4],
            }
            for r in rows
        ])
    db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).delete()
    db.add(SyncSourceSnapshotMeta(
        jobId=job_id, initialized=1, scanTime=scan_time, entryCount=len(rows)
    ))
    return {"jobId": int(job_id), "initialized": 1, "scanTime": scan_time, "entryCount": len(rows)}


def clear_source_snapshot(db, job_id: int) -> None:
    db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
    db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).delete()


def clear_source_snapshots_by_engine(db, engine_id: int) -> None:
    job_ids = [r[0] for r in db.query(SyncJob.id).filter_by(engineId=engine_id).all()]
    if not job_ids:
        return
    db.query(SyncSourceSnapshot).filter(SyncSourceSnapshot.jobId.in_(job_ids)).delete(
        synchronize_session=False
    )
    db.query(SyncSourceSnapshotMeta).filter(
        SyncSourceSnapshotMeta.jobId.in_(job_ids)
    ).delete(synchronize_session=False)


# ---------- 任务（task） ----------

def update_job_task_status_by_status(db) -> None:
    db.query(SyncTask).filter(SyncTask.status.in_((0, 1))).update(
        {"status": 4}, synchronize_session=False
    )


def update_job_task_status_by_status_and_job_id(db, job_id: int) -> None:
    db.query(SyncTask).filter(
        SyncTask.status.in_((0, 1)), SyncTask.jobId == job_id
    ).update({"status": 4}, synchronize_session=False)


def add_job_task(db, job_task: dict) -> int:
    row = SyncTask(**{k: v for k, v in job_task.items() if k != "id"})
    db.add(row)
    db.flush()
    return row.id


def update_job_task_status(db, task_id: int, status: int, err_msg=None) -> None:
    row = db.query(SyncTask).filter_by(id=task_id).first()
    if row is not None:
        row.status = status
        if err_msg is not None:
            row.errMsg = err_msg


def get_job_task_list(db, params: dict):
    job_id = int(params["id"])
    page = int(params.get("pageNum", 1))
    size = int(params.get("pageSize", 20))
    query = db.query(SyncTask).filter_by(jobId=job_id)
    total = query.count()
    rows = query.order_by(SyncTask.runTime.desc()).limit(size).offset((page - 1) * size).all()
    return {"dataList": rows, "total": total, "pageNum": page, "pageSize": size}


def get_job_task_count_by_status(db, task_id: int, status: int) -> int:
    return db.query(SyncTaskItem).filter_by(taskId=task_id, status=status).count()


def get_job_task_count_by_other(db, task_id: int) -> int:
    return db.query(SyncTaskItem).filter(
        SyncTaskItem.taskId == task_id, SyncTaskItem.status.notin_((0, 1, 2, 7))
    ).count()


def get_job_task_count_by_all(db, task_id: int) -> int:
    return db.query(SyncTaskItem).filter_by(taskId=task_id).count()


def delete_job_task_by_task_id(db, task_id: int) -> None:
    db.query(SyncTaskItem).filter_by(taskId=task_id).delete()
    db.query(SyncTask).filter_by(id=task_id).delete()


def delete_job_task_by_run_time(db, run_time: int) -> None:
    task_ids = [r[0] for r in db.query(SyncTask.id).filter(SyncTask.runTime < run_time).all()]
    if task_ids:
        db.query(SyncTaskItem).filter(SyncTaskItem.taskId.in_(task_ids)).delete(
            synchronize_session=False
        )
        db.query(SyncTask).filter(SyncTask.id.in_(task_ids)).delete(synchronize_session=False)


def update_job_task_num_many(db, task_nums: list) -> None:
    """批量更新任务结果统计（taskNum JSON）。"""
    for item in task_nums:
        row = db.query(SyncTask).filter_by(id=item["taskId"]).first()
        if row is not None:
            row.taskNum = item["taskNum"]


def add_job_task_item_many(db, items: list) -> None:
    db.bulk_insert_mappings(SyncTaskItem, [
        {
            "taskId": it["taskId"],
            "srcPath": it.get("srcPath", ""),
            "dstPath": it.get("dstPath", ""),
            "isPath": it.get("isPath", 0),
            "fileName": it.get("fileName", ""),
            "fileSize": it.get("fileSize"),
            "type": it.get("type", 0),
            "alistTaskId": str(it.get("alistTaskId", "")),
            "status": it.get("status", 0),
            "progress": it.get("progress", 0),
            "errMsg": it.get("errMsg", ""),
        }
        for it in items
    ])


def get_job_task_item_list(db, params: dict):
    task_id = int(params["taskId"])
    query = db.query(SyncTaskItem).filter_by(taskId=task_id)
    if "status" in params:
        query = query.filter_by(status=params["status"])
    if "type" in params:
        query = query.filter_by(type=params["type"])
    page = int(params.get("pageNum", 1))
    size = int(params.get("pageSize", 50))
    total = query.count()
    rows = query.order_by(SyncTaskItem.createTime.desc()).limit(size).offset((page - 1) * size).all()
    return {"dataList": rows, "total": total, "pageNum": page, "pageSize": size}


# ---------- 移动日志（move_log） ----------

def load_moved_file_names(db, job_id: int) -> set:
    """读取移动日志，返回已移动文件名集合（仅文件名，不含目录）。"""
    rows = db.query(SyncMoveLog.fileName).filter_by(jobId=job_id).all()
    return {r[0] for r in rows if r[0]}


def append_moved_file(db, job_id: int, file_name: str, src_path=None) -> None:
    if not file_name:
        return
    db.add(SyncMoveLog(
        jobId=job_id,
        fileName=file_name,
        srcPath=src_path or "",
        createTime=int(time.time()),
    ))
