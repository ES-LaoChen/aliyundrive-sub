"""作业 / 任务 / 快照 DAO（移植自 TaoSync mapper/jobMapper.py，改用 SQLAlchemy ORM）。

原 ``jobMapper`` 以 raw sqlite3 字符串 SQL 实现；这里全部翻译为对 ``models_sync``
中 ORM 实体的会话工厂操作，列名沿用原始 camelCase 列名。注意 jobMapper 的
核心契约（含 SOURCE_SNAPSHOT_FIELDS 与任务计数聚合）必须保持等价。
"""
from __future__ import annotations

import time
from typing import Optional

from db import get_session_local

from models_sync import (
    SyncJob,
    SyncMoveLog,
    SyncRecord,
    SyncSourceSnapshot,
    SyncSourceSnapshotMeta,
    SyncTask,
    SyncTaskItem,
)

SOURCE_SNAPSHOT_FIELDS = (
    'alistId', 'srcPath', 'dstPath', 'method', 'exclude',
    'minFileSize', 'maxFileSize',
)


def source_snapshot_identity(job):
    identity = {key: job.get(key) for key in SOURCE_SNAPSHOT_FIELDS}
    for key in ('alistId', 'method', 'minFileSize', 'maxFileSize'):
        value = identity[key]
        if value is not None:
            try:
                identity[key] = int(value)
            except (TypeError, ValueError):
                pass
    return identity


# ───────────────────────── 作业 job ─────────────────────────
def get_job_list(page_size=None, page_num=None, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        q = db.query(SyncJob).order_by(SyncJob.createTime.desc())
        rows = q.all()
        data = [_job_to_dict(r) for r in rows]
    if page_size:  # 兼容源项目分页结构
        page_num = page_num or 1
        start = (page_num - 1) * page_size
        page = data[start:start + page_size]
        return {
            'dataList': page,
            'pageNum': page_num,
            'pageSize': page_size,
            'total': len(data),
        }
    return data


def get_enable_job_list(session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        rows = db.query(SyncJob).filter(SyncJob.enable == 1).all()
        return [_job_to_dict(r) for r in rows]


def get_job_by_id(job_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        row = db.get(SyncJob, int(job_id))
        if row is None:
            raise Exception("job not found")
        return _job_to_dict(row)


def get_job_by_task_id(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        task = db.get(SyncTask, int(task_id))
        if task is None:
            raise Exception("task not found")
        row = db.get(SyncJob, int(task.jobId))
        if row is None:
            raise Exception("job not found")
        return _job_to_dict(row)


def add_job(job, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = SyncJob(
            enable=int(job.get('enable', 1)),
            remark=job.get('remark') or "",
            srcPath=job.get('srcPath') or "",
            dstPath=job.get('dstPath') or "",
            alistId=int(job.get('alistId') or 0),
            useCacheT=int(job.get('useCacheT', 1)),
            scanIntervalT=int(job.get('scanIntervalT', 0)),
            useCacheS=int(job.get('useCacheS', 1)),
            scanIntervalS=int(job.get('scanIntervalS', 0)),
            method=int(job.get('method', 0)),
            sourceMode=int(job.get('sourceMode', 0)),
            interval=int(job.get('interval', 0) or 0),
            isCron=int(job.get('isCron', 0)),
            year=job.get('year') or "",
            month=job.get('month') or "",
            day=job.get('day') or "",
            week=job.get('week') or "",
            day_of_week=job.get('day_of_week') or "",
            hour=job.get('hour') or "",
            minute=job.get('minute') or "",
            second=job.get('second') or "",
            start_date=job.get('start_date') or "",
            end_date=job.get('end_date') or "",
            exclude=job.get('exclude') or "",
            minFileSize=job.get('minFileSize'),
            maxFileSize=job.get('maxFileSize'),
            createTime=int(time.time()),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id


def update_job(job, clear_source_snapshot=False, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = db.get(SyncJob, int(job['id']))
        if obj is None:
            raise Exception("job not found")
        obj.enable = int(job.get('enable', obj.enable))
        obj.remark = job.get('remark', obj.remark)
        obj.srcPath = job.get('srcPath', obj.srcPath)
        obj.dstPath = job.get('dstPath', obj.dstPath)
        obj.alistId = int(job.get('alistId', obj.alistId))
        obj.useCacheT = int(job.get('useCacheT', obj.useCacheT))
        obj.scanIntervalT = int(job.get('scanIntervalT', obj.scanIntervalT))
        obj.useCacheS = int(job.get('useCacheS', obj.useCacheS))
        obj.scanIntervalS = int(job.get('scanIntervalS', obj.scanIntervalS))
        obj.method = int(job.get('method', obj.method))
        obj.sourceMode = int(job.get('sourceMode', obj.sourceMode))
        obj.interval = int(job.get('interval', obj.interval) or 0)
        obj.isCron = int(job.get('isCron', obj.isCron))
        obj.year = job.get('year', obj.year) or ""
        obj.month = job.get('month', obj.month) or ""
        obj.day = job.get('day', obj.day) or ""
        obj.week = job.get('week', obj.week) or ""
        obj.day_of_week = job.get('day_of_week', obj.day_of_week) or ""
        obj.hour = job.get('hour', obj.hour) or ""
        obj.minute = job.get('minute', obj.minute) or ""
        obj.second = job.get('second', obj.second) or ""
        obj.start_date = job.get('start_date', obj.start_date) or ""
        obj.end_date = job.get('end_date', obj.end_date) or ""
        obj.exclude = job.get('exclude', obj.exclude) or ""
        obj.minFileSize = job.get('minFileSize', obj.minFileSize)
        obj.maxFileSize = job.get('maxFileSize', obj.maxFileSize)
        if clear_source_snapshot:
            db.query(SyncSourceSnapshot).filter_by(jobId=obj.id).delete()
            db.query(SyncSourceSnapshotMeta).filter_by(jobId=obj.id).delete()
        db.commit()


def update_job_enable(job_id, enable, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = db.get(SyncJob, int(job_id))
        if obj is not None:
            obj.enable = int(enable)
            db.commit()


def delete_job(job_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        job_id = int(job_id)
        db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
        db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).delete()
        db.query(SyncTaskItem).filter(
            SyncTaskItem.taskId.in_(
                db.query(SyncTask.id).filter_by(jobId=job_id).subquery())
        ).delete(synchronize_session=False)
        db.query(SyncTask).filter_by(jobId=job_id).delete()
        db.query(SyncJob).filter_by(id=job_id).delete()
        # 同步移动日志与运行记录一并清理
        db.query(SyncMoveLog).filter_by(jobId=job_id).delete()
        db.query(SyncRecord).filter_by(jobId=job_id).delete()
        db.commit()


# ───────────────────────── 源快照 snapshot ─────────────────────────
def get_source_snapshot(job_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        meta_row = db.get(SyncSourceSnapshotMeta, int(job_id))
        if meta_row is not None:
            meta = {
                'jobId': meta_row.jobId,
                'initialized': meta_row.initialized,
                'scanTime': meta_row.scanTime,
                'entryCount': meta_row.entryCount,
            }
        else:
            meta = {
                'jobId': int(job_id),
                'initialized': 0,
                'scanTime': None,
                'entryCount': 0,
            }
        entry_rows = (
            db.query(SyncSourceSnapshot)
            .filter_by(jobId=int(job_id))
            .order_by(SyncSourceSnapshot.path.asc())
            .all()
        )
        entries = []
        for e in entry_rows:
            entry = {'path': e.path, 'isDir': e.isDir, 'size': e.size}
            if e.fingerprint is not None:
                entry['fingerprint'] = e.fingerprint
            entries.append(entry)
        return {'meta': meta, 'entries': entries}


def replace_source_snapshot(job_id, entries, expected_identity=None, session_factory=None):
    sf = session_factory or get_session_local()
    job_id = int(job_id)
    rows_by_path = {}
    for entry in entries:
        path = entry['path']
        if not isinstance(path, str):
            raise ValueError('snapshot path must be a string')
        is_dir = 1 if entry.get('isDir', entry.get('is_dir', 0)) else 0
        fingerprint = entry.get('fingerprint')
        if fingerprint is not None:
            fingerprint = str(fingerprint)
        rows_by_path[path] = (
            job_id, path, is_dir,
            None if is_dir else entry.get('size'),
            fingerprint,
        )
    rows = list(rows_by_path.values())
    scan_time = int(time.time())
    with sf() as db:
        job_row = db.get(SyncJob, job_id)
        if job_row is None:
            raise Exception("job not found")
        if expected_identity is not None:
            current = source_snapshot_identity(_job_to_dict(job_row))
            if current != expected_identity:
                raise RuntimeError("job changed during sync")
        db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
        if rows:
            db.bulk_insert_mappings(SyncSourceSnapshot, [
                {
                    'jobId': r[0], 'path': r[1], 'isDir': r[2],
                    'size': r[3], 'fingerprint': r[4],
                } for r in rows
            ])
        db.merge(SyncSourceSnapshotMeta(
            jobId=job_id, initialized=1, scanTime=scan_time, entryCount=len(rows)))
        db.commit()
    return {
        'jobId': job_id,
        'initialized': 1,
        'scanTime': scan_time,
        'entryCount': len(rows),
    }


def clear_source_snapshot(job_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        job_id = int(job_id)
        db.query(SyncSourceSnapshot).filter_by(jobId=job_id).delete()
        db.query(SyncSourceSnapshotMeta).filter_by(jobId=job_id).delete()
        db.commit()


def clear_source_snapshots_by_engine(engine_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        engine_id = int(engine_id)
        job_ids = [r[0] for r in db.query(SyncJob.id).filter_by(alistId=engine_id).all()]
        if not job_ids:
            return
        db.query(SyncSourceSnapshot).filter(
            SyncSourceSnapshot.jobId.in_(job_ids)).delete(synchronize_session=False)
        db.query(SyncSourceSnapshotMeta).filter(
            SyncSourceSnapshotMeta.jobId.in_(job_ids)).delete(synchronize_session=False)
        db.commit()


# ───────────────────────── 任务 task ─────────────────────────
def get_latest_job_task_list(job_ids, session_factory=None):
    """返回每个作业最新一次运行的聚合计数（status 2=成功, 7=失败）。"""
    sf = session_factory or get_session_local()
    job_ids = [int(j) for j in job_ids]
    if not job_ids:
        return []
    with sf() as db:
        result = []
        for job_id in job_ids:
            task = (
                db.query(SyncTask)
                .filter_by(jobId=job_id)
                .order_by(SyncTask.createTime.desc(), SyncTask.id.desc())
                .first()
            )
            if task is None:
                result.append({'jobId': job_id, 'task': None})
                continue
            items = db.query(SyncTaskItem).filter_by(taskId=task.id).all()
            item_all = len(items)
            item_success = sum(1 for i in items if i.status == 2)
            item_fail = sum(1 for i in items if i.status == 7)
            item_other = sum(1 for i in items if i.status not in (0, 1, 2, 7))
            result.append({
                'jobId': job_id,
                'id': task.id,
                'jobId': job_id,
                'status': task.status,
                'errMsg': task.errMsg,
                'runTime': task.runTime,
                'taskNum': task.taskNum,
                'createTime': task.createTime,
                'itemAllNum': item_all,
                'itemSuccessNum': item_success,
                'itemFailNum': item_fail,
                'itemOtherNum': item_other,
            })
        return result


def get_job_task_list(job_id, page_size=None, page_num=None, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        rows = (
            db.query(SyncTask)
            .filter_by(jobId=int(job_id))
            .order_by(SyncTask.createTime.desc())
            .all()
        )
        data = [_task_to_dict(r) for r in rows]
    if page_size:
        page_num = page_num or 1
        start = (page_num - 1) * page_size
        page = data[start:start + page_size]
        return {
            'dataList': page,
            'pageNum': page_num,
            'pageSize': page_size,
            'total': len(data),
        }
    return data


def get_job_task_count_by_status(task_id, status, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        return db.query(SyncTaskItem).filter_by(taskId=int(task_id), status=status).count()


def get_job_task_count_by_other(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        return db.query(SyncTaskItem).filter(
            SyncTaskItem.taskId == int(task_id),
            SyncTaskItem.status.notin_([0, 1, 2, 7]),
        ).count()


def get_job_task_count_by_all(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        return db.query(SyncTaskItem).filter_by(taskId=int(task_id)).count()


def get_job_task_by_id(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        row = db.get(SyncTask, int(task_id))
        if row is None:
            raise Exception("task not found")
        return _task_to_dict(row)


def add_job_task(task, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = SyncTask(
            jobId=int(task['jobId']),
            runTime=int(task.get('runTime', time.time())),
            status=int(task.get('status', 0)),
            errMsg=task.get('errMsg') or "",
            taskNum=task.get('taskNum') or "",
            createTime=int(time.time()),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id


def update_job_task_status(task_id, status, err_msg=None, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = db.get(SyncTask, int(task_id))
        if obj is not None:
            obj.status = int(status)
            if err_msg is not None:
                obj.errMsg = err_msg
            db.commit()


def update_job_task_num_many(task_nums, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        for tn in task_nums:
            obj = db.get(SyncTask, int(tn['taskId']))
            if obj is not None:
                obj.taskNum = tn['taskNum']
        db.commit()


def update_job_task_status_by_status(session_factory=None):
    """重启后把未完成的任务标记为中止（status=4）。"""
    sf = session_factory or get_session_local()
    with sf() as db:
        db.query(SyncTask).filter(SyncTask.status.in_([0, 1])).update(
            {SyncTask.status: 4}, synchronize_session=False)
        db.commit()


def update_job_task_status_by_status_and_job_id(job_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        db.query(SyncTask).filter_by(jobId=int(job_id)).filter(
            SyncTask.status.in_([0, 1])).update(
            {SyncTask.status: 4}, synchronize_session=False)
        db.commit()


def delete_job_task_by_task_id(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        task_id = int(task_id)
        db.query(SyncTaskItem).filter_by(taskId=task_id).delete()
        db.query(SyncTask).filter_by(id=task_id).delete()
        db.commit()


def delete_job_task_by_run_time(run_time, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        task_ids = [r[0] for r in db.query(SyncTask.id).filter(
            SyncTask.runTime < int(run_time)).all()]
        if task_ids:
            db.query(SyncTaskItem).filter(
                SyncTaskItem.taskId.in_(task_ids)).delete(synchronize_session=False)
            db.query(SyncTask).filter(SyncTask.id.in_(task_ids)).delete(
                synchronize_session=False)
            db.commit()


# ───────────────────────── 任务条目 task_item ─────────────────────────
def add_job_task_item_many(items, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        mappings = []
        for it in items:
            mappings.append({
                'taskId': int(it['taskId']),
                'srcPath': it.get('srcPath') or "",
                'dstPath': it.get('dstPath') or "",
                'isPath': int(it.get('isPath', 0)),
                'fileName': it.get('fileName') or "",
                'fileSize': int(it.get('fileSize') or 0),
                'type': int(it.get('type', 0)),
                'alistTaskId': it.get('alistTaskId') or "",
                'status': int(it.get('status', 0)),
                'errMsg': it.get('errMsg') or "",
                'progress': int(it.get('progress', 0) or 0),
                'createTime': int(it.get('createTime', time.time())),
            })
        db.bulk_insert_mappings(SyncTaskItem, mappings)
        db.commit()


def get_job_task_item_list(task_id, status=None, item_type=None, page_size=None,
                           page_num=None, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        q = db.query(SyncTaskItem).filter_by(taskId=int(task_id))
        if status is not None:
            q = q.filter_by(status=int(status))
        if item_type is not None:
            q = q.filter_by(type=int(item_type))
        rows = q.order_by(SyncTaskItem.createTime.desc()).all()
        data = [_task_item_to_dict(r) for r in rows]
    if page_size:
        page_num = page_num or 1
        start = (page_num - 1) * page_size
        page = data[start:start + page_size]
        return {
            'dataList': page,
            'pageNum': page_num,
            'pageSize': page_size,
            'total': len(data),
        }
    return data


def get_undone_job_task_item_list(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        rows = db.query(SyncTaskItem).filter_by(taskId=int(task_id)).filter(
            SyncTaskItem.status.notin_([2, 4, 7])).all()
        return [_task_item_to_dict(r) for r in rows]


def get_unsuccess_job_task_item_list(task_id, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        rows = db.query(SyncTaskItem).filter_by(taskId=int(task_id)).filter(
            SyncTaskItem.status != 2).all()
        return [_task_item_to_dict(r) for r in rows]


def update_job_task_item_status_by_id_many(task_list, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        for t in task_list:
            obj = db.get(SyncTaskItem, int(t['id']))
            if obj is not None:
                obj.status = int(t.get('status', obj.status))
                obj.progress = int(t.get('progress', obj.progress) or 0)
                obj.errMsg = t.get('errMsg', obj.errMsg) or ""
        db.commit()


# ───────────────────────── 运行记录 sync_records ─────────────────────────
def add_sync_record(record, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        obj = SyncRecord(
            jobId=int(record.get('jobId', 0)),
            jobName=record.get('jobName') or "",
            operator=record.get('operator') or "",
            status=int(record.get('status', 0)),
            dataCount=int(record.get('dataCount', 0)),
            dataSize=int(record.get('dataSize', 0)),
            errMsg=record.get('errMsg') or "",
            startTime=int(record.get('startTime', 0)),
            endTime=int(record.get('endTime', 0)),
            createTime=int(time.time()),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id


def get_sync_record_list(job_id=None, page_size=None, page_num=None,
                         session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        q = db.query(SyncRecord)
        if job_id is not None:
            q = q.filter_by(jobId=int(job_id))
        rows = q.order_by(SyncRecord.createTime.desc()).all()
        data = [_record_to_dict(r) for r in rows]
    if page_size:
        page_num = page_num or 1
        start = (page_num - 1) * page_size
        page = data[start:start + page_size]
        return {
            'dataList': page,
            'pageNum': page_num,
            'pageSize': page_size,
            'total': len(data),
        }
    return data


def clear_sync_records_before(end_time, session_factory=None):
    sf = session_factory or get_session_local()
    with sf() as db:
        db.query(SyncRecord).filter(SyncRecord.createTime < int(end_time)).delete(
            synchronize_session=False)
        db.commit()


# ───────────────────────── 序列化的辅助 ─────────────────────────
def _job_to_dict(row):
    d = {}
    for col in row.__table__.columns:
        d[col.name] = getattr(row, col.name)
    return d


def _task_to_dict(row):
    return _job_to_dict(row)


def _task_item_to_dict(row):
    return _job_to_dict(row)


def _record_to_dict(row):
    return _job_to_dict(row)
