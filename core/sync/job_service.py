"""同步作业服务（移植自 TaoSync service/syncJob/jobService.py）。

适配差异：
- 客户端缓存为进程内 dict（``jobClientList``），与 TaoSync 同语义。
- ``cleanJobInput`` 引用 ``engineService.getClientById`` → 改由调用方传入
  ``session_factory``，在需要重叠检查时走 ``core.sync_storage.engine``。
- 异常文案 ``common.LNG.G(...)`` → 中文字面量。
"""
from __future__ import annotations

import json
import logging

from core.sync import job_client, job_dao
from core.sync.job_client_helpers import (
    normalize_file_size,
    normalize_source_mode,
    virtual_paths_overlap,
)

logger = logging.getLogger(__name__)

# 作业客户端列表，key 为 jobId，value 为 JobClient
jobClientList = {}

SOURCE_SNAPSHOT_FIELDS = job_dao.SOURCE_SNAPSHOT_FIELDS


def _engine_paths_overlap(engine_id, first_path, second_path, session_factory):
    if engine_id is None:
        return virtual_paths_overlap(first_path, second_path)
    from core.sync_storage.engine import get_client_by_id
    client = get_client_by_id(engine_id, session_factory)
    paths_overlap = getattr(client, 'pathsOverlap', virtual_paths_overlap)
    return paths_overlap(first_path, second_path)


def clean_job_input(job, session_factory):
    if job['isCron'] == 2 and job.get('enable') != 1:
        job['enable'] = 1
    for key, value in job.items():
        if isinstance(value, str):
            if value.strip() == '':
                job[key] = None
            else:
                job[key] = value.strip()
    if job.get('exclude') is not None:
        job['exclude'] = ":".join([item.strip() for item in job['exclude'].split(':')])
    job.setdefault('minFileSize', None)
    job.setdefault('maxFileSize', None)
    job.setdefault('sourceMode', 0)
    job['minFileSize'] = normalize_file_size(job['minFileSize'])
    job['maxFileSize'] = normalize_file_size(job['maxFileSize'])
    job['sourceMode'] = normalize_source_mode(job['sourceMode'])
    if job.get('srcPath') and job.get('dstPath'):
        for dstPath in job['dstPath'].split(':'):
            if _engine_paths_overlap(job.get('alistId'), job['srcPath'], dstPath,
                                     session_factory):
                raise Exception("来源与目标路径存在重叠，已拒绝保存")
    if (job['minFileSize'] is not None
            and job['maxFileSize'] is not None
            and job['minFileSize'] > job['maxFileSize']):
        raise Exception("最小文件大小不能大于最大文件大小")


def init_jobs(session_factory, notifier=None):
    """启动时恢复异常终止状态的任务，并启动所有启用作业。"""
    job_dao.update_job_task_status_by_status(session_factory)
    job_list = job_dao.get_job_list(session_factory)
    rows = job_list['dataList'] if isinstance(job_list, dict) else job_list
    for item in rows:
        try:
            logger.info("正在添加同步作业 jobId=%s", item['id'])
            add_job_client(item, True, session_factory, notifier)
        except Exception:
            logger.exception("添加同步作业过程出错 jobId=%s", item.get('id'))


def get_job_client_by_id(job_id, session_factory, notifier=None):
    job_id = int(job_id)
    global jobClientList
    if job_id in jobClientList:
        return jobClientList[job_id]
    job = job_dao.get_job_by_id(job_id, session_factory)
    client = job_client.JobClient(job, False, notifier, session_factory)
    jobClientList[job_id] = client
    return client


def add_job_client(job, is_init=False, session_factory=None, notifier=None):
    clean_job_input(job, session_factory)
    client = job_client.JobClient(job, is_init, notifier, session_factory)
    global jobClientList
    jobClientList[int(client.jobId)] = client
    return client.jobId


def edit_job_client(job, session_factory=None, notifier=None):
    job_id = int(job['id'])
    clean_job_input(job, session_factory)
    client = get_job_client_by_id(job_id, session_factory, notifier)
    if client.job['enable'] == 1 and client.job['isCron'] != 2:
        raise Exception("请先禁用作业再编辑")
    clear_snapshot = any(client.job.get(key) != job.get(key) for key in SOURCE_SNAPSHOT_FIELDS)
    old_job = client.job.copy()
    client.stopJob(remove=True)
    global jobClientList
    new_client = None
    try:
        new_client = job_client.JobClient(job, False, notifier, session_factory)
        job_dao.update_job(job, clear_source_snapshot=clear_snapshot, session_factory=session_factory)
    except Exception:
        if new_client is not None:
            new_client.stopJob(remove=True)
        try:
            jobClientList[job_id] = job_client.JobClient(old_job, False, notifier, session_factory)
        except Exception as restore_error:
            logger.exception(restore_error)
        raise
    jobClientList[job_id] = new_client


def do_all_job_manual(session_factory=None, notifier=None):
    job_list = job_dao.get_enable_job_list(session_factory)
    if not job_list:
        raise Exception("没有可运行的启用作业")
    for job_item in job_list:
        client = get_job_client_by_id(job_item['id'], session_factory, notifier)
        if client.job['enable'] == 1:
            client.doManual()


def pause_all_job(session_factory=None, notifier=None):
    job_list = job_dao.get_job_list(session_factory)
    rows = job_list['dataList'] if isinstance(job_list, dict) else job_list
    for job in rows:
        if job.get('isCron') == 2 or job.get('enable') != 1:
            continue
        pause_job(job['id'], session_factory, notifier)


def continue_all_job(session_factory=None, notifier=None):
    job_list = job_dao.get_job_list(session_factory)
    rows = job_list['dataList'] if isinstance(job_list, dict) else job_list
    for job in rows:
        if job.get('isCron') == 2 or job.get('enable') == 1:
            continue
        continue_job(job['id'], session_factory, notifier)


def do_job_manual(job_id, session_factory=None, notifier=None):
    client = get_job_client_by_id(job_id, session_factory, notifier)
    if client.job['enable'] != 1:
        raise Exception("已禁用的作业无法运行")
    client.doManual()


def remove_job_client(job_id, session_factory=None, notifier=None):
    job_id = int(job_id)
    client = get_job_client_by_id(job_id, session_factory, notifier)
    client.stopJob(remove=True)
    job_dao.delete_job(job_id, session_factory)
    global jobClientList
    jobClientList.pop(job_id, None)


def continue_job(job_id, session_factory=None, notifier=None):
    client = get_job_client_by_id(job_id, session_factory, notifier)
    client.resumeJob()


def pause_job(job_id, session_factory=None, notifier=None):
    client = get_job_client_by_id(job_id, session_factory, notifier)
    if client.job['isCron'] == 2:
        raise Exception("手动作业不可禁用")
    client.stopJob()


def abort_job(job_id, session_factory=None, notifier=None):
    client = get_job_client_by_id(job_id, session_factory, notifier)
    client.abortJob()


def get_job_list_view(req, session_factory=None):
    result = job_dao.get_job_list(
        page_size=int(req.get('pageSize', 0)) or None,
        page_num=int(req.get('pageNum', 1)) or 1,
        session_factory=session_factory,
    )
    rows = result.get('dataList', result) if isinstance(result, dict) else result
    latest_tasks = {
        task['jobId']: task for task in job_dao.get_latest_job_task_list(
            [row['id'] for row in rows], session_factory)
    }
    for row in rows:
        task = latest_tasks.get(row['id'])
        if task is None or task.get('task') is None:
            row['lastTask'] = None
            continue
        task_num = {}
        if task.get('taskNum'):
            try:
                parsed = json.loads(task['taskNum'])
                if isinstance(parsed, dict):
                    task_num = parsed
            except (TypeError, ValueError):
                pass
        task['allNum'] = task_num.get('allNum', task.get('itemAllNum') or 0)
        task['successNum'] = task_num.get('successNum', task.get('itemSuccessNum') or 0)
        task['failNum'] = task_num.get('failNum', task.get('itemFailNum') or 0)
        task['otherNum'] = task_num.get('otherNum', task.get('itemOtherNum') or 0)
        task['duration'] = task_num.get('duration')
        for k in ('taskNum', 'itemAllNum', 'itemSuccessNum', 'itemFailNum', 'itemOtherNum'):
            task.pop(k, None)
        row['lastTask'] = task
    return result


def get_job_current(job_id, status=None, session_factory=None, notifier=None):
    client = get_job_client_by_id(int(job_id), session_factory, notifier)
    task_client = client.currentJobTask
    if task_client is not None:
        if status is None:
            return task_client.getCurrent()
        return task_client.getCurrentByStatus(int(status))
    return None


def validate_mounts_exist(session_factory):
    """内置 taosync 引擎必须至少存在一个 local 挂载，job 才能正常执行。"""
    from core.sync_storage.engine import StorageMountDAO
    dao = StorageMountDAO(session_factory)
    engine = _get_taosync_engine(session_factory)
    if engine is None:
        return False
    return len(dao.get_mount_list(engine['id'])) > 0


def _get_taosync_engine(session_factory):
    from core.sync_storage.engine import SyncEngineDAO
    for e in SyncEngineDAO(session_factory).get_engine_list():
        if e.get('engineType') == 'taosync' and e.get('systemKey') == 'taosync':
            return e
    return None
