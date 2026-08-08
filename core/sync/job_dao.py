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
    SyncProgress,
    SyncRecord,
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


# ---------- 同步记录（sync_record） ----------

def add_sync_record(db, record: dict) -> int:
    """写入一条同步操作历史记录，返回自增 id。"""
    row = SyncRecord(**{k: v for k, v in record.items() if k != "id"})
    db.add(row)
    db.flush()
    return row.id


def get_sync_record_list(db, params: dict) -> dict:
    """分页 + 过滤查询同步记录。

    过滤参数（均在 params 中，按需提供）：
      - jobId:       按作业 id 过滤
      - status:      按状态过滤（整数）
      - operator:    按操作人员/触发来源模糊匹配
      - startTimeFrom / startTimeTo: 按 startTime（Unix 秒）范围过滤
      - pageNum / pageSize: 分页，默认 1 / 20
    返回 {dataList, total, pageNum, pageSize}。
    """
    query = db.query(SyncRecord)
    if params.get("jobId"):
        try:
            query = query.filter_by(jobId=int(params["jobId"]))
        except (TypeError, ValueError):
            pass
    if params.get("status") not in (None, ""):
        try:
            query = query.filter_by(status=int(params["status"]))
        except (TypeError, ValueError):
            pass
    operator = (params.get("operator") or "").strip()
    if operator:
        query = query.filter(SyncRecord.operator.like(f"%{operator}%"))
    try:
        sf = int(params.get("startTimeFrom") or 0)
    except (TypeError, ValueError):
        sf = 0
    if sf > 0:
        query = query.filter(SyncRecord.startTime >= sf)
    try:
        st = int(params.get("startTimeTo") or 0)
    except (TypeError, ValueError):
        st = 0
    if st > 0:
        query = query.filter(SyncRecord.startTime <= st)

    page = int(params.get("pageNum", 1) or 1)
    size = int(params.get("pageSize", 20) or 20)
    total = query.count()
    rows = (
        query.order_by(SyncRecord.startTime.desc(), SyncRecord.id.desc())
        .limit(size)
        .offset((page - 1) * size)
        .all()
    )
    return {"dataList": rows, "total": total, "pageNum": page, "pageSize": size}


def get_all_sync_records(db, params: dict = None) -> list:
    """导出用：返回符合条件的全部记录（不分页），按时间倒序。"""
    params = params or {}
    query = db.query(SyncRecord)
    if params.get("jobId"):
        try:
            query = query.filter_by(jobId=int(params["jobId"]))
        except (TypeError, ValueError):
            pass
    if params.get("status") not in (None, ""):
        try:
            query = query.filter_by(status=int(params["status"]))
        except (TypeError, ValueError):
            pass
    operator = (params.get("operator") or "").strip()
    if operator:
        query = query.filter(SyncRecord.operator.like(f"%{operator}%"))
    try:
        sf = int(params.get("startTimeFrom") or 0)
    except (TypeError, ValueError):
        sf = 0
    if sf > 0:
        query = query.filter(SyncRecord.startTime >= sf)
    try:
        st = int(params.get("startTimeTo") or 0)
    except (TypeError, ValueError):
        st = 0
    if st > 0:
        query = query.filter(SyncRecord.startTime <= st)
    return query.order_by(SyncRecord.startTime.desc(), SyncRecord.id.desc()).all()


# ---------- 实时进度（sync_progress） ----------

# 进度状态语义（与 SyncProgress.status 对齐）。
PROGRESS_STATUS_PENDING = 0
PROGRESS_STATUS_RUNNING = 1
PROGRESS_STATUS_SUCCESS = 2
PROGRESS_STATUS_ABORTED = 4
PROGRESS_STATUS_FAILED = 7


def upsert_progress(db, task_id: int, job_id: int, item: dict) -> None:
    """单条 upsert 进度（按 (taskId, fileName, srcPath) 唯一键）。

    大规模同步时请改用 :func:`bulk_upsert_progress` 批量节流写入。
    """
    now = int(time.time())
    existing = (
        db.query(SyncProgress)
        .filter_by(taskId=task_id, fileName=item.get("fileName", ""),
                   srcPath=item.get("srcPath", ""))
        .first()
    )
    if existing is None:
        db.add(SyncProgress(
            jobId=job_id, taskId=task_id,
            fileName=item.get("fileName", ""),
            srcPath=item.get("srcPath", ""),
            dstPath=item.get("dstPath", ""),
            fileSize=item.get("fileSize"),
            status=item.get("status", 0),
            progress=item.get("progress", 0),
            speed=item.get("speed", 0),
            transferred=item.get("transferred", 0),
            startedAt=item.get("startedAt", now),
            updatedAt=now,
            finishedAt=item.get("finishedAt", 0),
            errMsg=item.get("errMsg", "") or "",
        ))
    else:
        existing.fileSize = item.get("fileSize", existing.fileSize)
        existing.dstPath = item.get("dstPath", existing.dstPath)
        existing.status = item.get("status", existing.status)
        existing.progress = item.get("progress", existing.progress)
        existing.speed = item.get("speed", existing.speed)
        existing.transferred = item.get("transferred", existing.transferred)
        existing.updatedAt = now
        if item.get("finishedAt"):
            existing.finishedAt = item.get("finishedAt")
        if item.get("errMsg"):
            existing.errMsg = item.get("errMsg", "")


def bulk_upsert_progress(db, task_id: int, job_id: int, items: list) -> None:
    """批量 upsert 进度（节流写入入口）。

    内部用 bulk_save_objects 减少 ORM 开销；单文件级 update 通过先查后写实现
    （已存在的按唯一键更新，不存在的新增）。大规模场景由调用方控制调用频率
    （如每 1s 或每 50 条 flush 一次），避免每文件一写。
    """
    if not items:
        return
    now = int(time.time())
    # 批量取出本 task 已存在记录建 map，减少 N+1 查询。
    existing_map = {}
    rows = db.query(SyncProgress).filter_by(taskId=task_id).all()
    for r in rows:
        existing_map[(r.fileName, r.srcPath)] = r
    new_objs = []
    for it in items:
        key = (it.get("fileName", ""), it.get("srcPath", ""))
        existing = existing_map.get(key)
        if existing is None:
            new_objs.append(SyncProgress(
                jobId=job_id, taskId=task_id,
                fileName=it.get("fileName", ""),
                srcPath=it.get("srcPath", ""),
                dstPath=it.get("dstPath", ""),
                fileSize=it.get("fileSize"),
                status=it.get("status", 0),
                progress=it.get("progress", 0),
                speed=it.get("speed", 0),
                transferred=it.get("transferred", 0),
                startedAt=it.get("startedAt", now),
                updatedAt=now,
                finishedAt=it.get("finishedAt", 0),
                errMsg=it.get("errMsg", "") or "",
            ))
        else:
            existing.fileSize = it.get("fileSize", existing.fileSize)
            existing.dstPath = it.get("dstPath", existing.dstPath)
            existing.status = it.get("status", existing.status)
            existing.progress = it.get("progress", existing.progress)
            existing.speed = it.get("speed", existing.speed)
            existing.transferred = it.get("transferred", existing.transferred)
            existing.updatedAt = now
            if it.get("finishedAt"):
                existing.finishedAt = it.get("finishedAt")
            if it.get("errMsg"):
                existing.errMsg = it.get("errMsg", "")
    if new_objs:
        db.add_all(new_objs)


def get_progress_summary(db, task_id: int, job_id: int = None) -> dict:
    """聚合进度统计（SQL 计数，不全表拉取，适合大规模文件）。

    返回：
      - total/running/success/failed/aborted/pending 计数
      - transferred 与 totalSize 字节汇总（近似，基于 fileSize）
      - percent：整体进度百分比（0-100），扫描未完成时基于已完成/(已完成+待处理)
      - recovered：本次运行基于「历史已移动/已完成」恢复的基数（见调用方传入）
    """
    from sqlalchemy import func

    q = db.query(SyncProgress.status, func.count(SyncProgress.id))
    if task_id:
        q = q.filter_by(taskId=task_id)
    elif job_id is not None:
        q = q.filter_by(jobId=job_id)
    counts = {status: cnt for status, cnt in q.group_by(SyncProgress.status).all()}

    running = counts.get(PROGRESS_STATUS_RUNNING, 0)
    success = counts.get(PROGRESS_STATUS_SUCCESS, 0)
    failed = counts.get(PROGRESS_STATUS_FAILED, 0)
    aborted = counts.get(PROGRESS_STATUS_ABORTED, 0)
    pending = counts.get(PROGRESS_STATUS_PENDING, 0)
    done = success + failed + aborted
    total = done + running + pending

    size_row = (
        db.query(
            func.coalesce(func.sum(SyncProgress.fileSize), 0),
            func.coalesce(func.sum(SyncProgress.transferred), 0),
        )
        .filter_by(taskId=task_id) if task_id else
        db.query(
            func.coalesce(func.sum(SyncProgress.fileSize), 0),
            func.coalesce(func.sum(SyncProgress.transferred), 0),
        ).filter_by(jobId=job_id)
    ).first()
    total_size = int(size_row[0] or 0)
    transferred_size = int(size_row[1] or 0)

    percent = 0
    if total > 0:
        percent = int(round(done * 100.0 / total))
    return {
        "total": total,
        "running": running,
        "success": success,
        "failed": failed,
        "aborted": aborted,
        "pending": pending,
        "done": done,
        "totalSize": total_size,
        "transferredSize": transferred_size,
        "percent": percent,
    }


def get_progress_active(db, task_id: int, limit: int = 20) -> list:
    """返回正在传输中的文件（最多 limit 条，供 UI 展示当前明细）。"""
    rows = (
        db.query(SyncProgress)
        .filter_by(taskId=task_id, status=PROGRESS_STATUS_RUNNING)
        .order_by(SyncProgress.updatedAt.desc())
        .limit(limit)
        .all()
    )
    return [{
        "fileName": r.fileName,
        "srcPath": r.srcPath,
        "dstPath": r.dstPath,
        "fileSize": r.fileSize,
        "progress": r.progress,
        "speed": r.speed,
        "transferred": r.transferred,
        "status": r.status,
        "errMsg": r.errMsg,
    } for r in rows]


def get_progress_recent_done(db, task_id: int, limit: int = 50) -> list:
    """返回最近完成的文件（成功/失败，最多 limit 条，供 UI 滚动展示）。"""
    rows = (
        db.query(SyncProgress)
        .filter(SyncProgress.taskId == task_id,
                SyncProgress.status.in_((PROGRESS_STATUS_SUCCESS, PROGRESS_STATUS_FAILED)))
        .order_by(SyncProgress.finishedAt.desc())
        .limit(limit)
        .all()
    )
    return [{
        "fileName": r.fileName,
        "srcPath": r.srcPath,
        "dstPath": r.dstPath,
        "fileSize": r.fileSize,
        "status": r.status,
        "errMsg": r.errMsg,
        "finishedAt": r.finishedAt,
    } for r in rows]


def count_progress_recovered(db, job_id: int, current_task_id: int) -> int:
    """恢复基数：上次运行中已成功（status=2）的文件数。

    用于「中断恢复显示」：本次运行开始前，先把历史已成功文件数计入进度分母，
    使得重启/中止后续跑时整体进度百分比能正确反映「跨运行累计完成度」。
    """
    return (
        db.query(SyncProgress)
        .filter_by(jobId=job_id, status=PROGRESS_STATUS_SUCCESS)
        .filter(SyncProgress.taskId != current_task_id)
        .count()
    )


def clear_progress_by_task(db, task_id: int) -> None:
    """清理指定 task 的进度记录（作业删除/历史清理时使用）。"""
    db.query(SyncProgress).filter_by(taskId=task_id).delete(
        synchronize_session=False
    )

