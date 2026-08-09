"""同步作业服务层：作业管理、调度注册、手动触发、启动续跑。

整合移植自 TaoSync ``service/syncJob/jobService.py``，持久化改为 SQLAlchemy。
通知钩子复用现有 ``core.notifier.NotifierManager``（Telegram），无配置则静默。
"""
from __future__ import annotations

import json
import logging

from core.sync import engine as storage_engine
from core.sync.job_client import JobClient, virtual_paths_overlap
from core.sync.job_dao import (
    clear_source_snapshot,
    delete_job,
    get_enable_job_list,
    get_job_by_id,
    get_job_by_task_id,
    get_job_list,
    get_latest_job_task_list,
    get_source_snapshot,
    source_snapshot_identity,
    update_job,
)

logger = logging.getLogger(__name__)

# 作业客户端进程内缓存（key=jobId）。
job_client_list: dict = {}

MAX_SQLITE_INTEGER = 9223372036854775807
SOURCE_SNAPSHOT_FIELDS = (
    "engineId", "srcPath", "dstPath", "method", "exclude", "minFileSize", "maxFileSize"
)


def normalize_file_size(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("file size invalid")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise ValueError("file size invalid")
    if result < 0 or result > MAX_SQLITE_INTEGER:
        raise ValueError("file size invalid")
    return result


def normalize_source_mode(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value in ("0", "1"):
        return int(value)
    raise ValueError("source mode invalid")


class SyncNotifyAdapter:
    """把同步任务结束事件转为 Telegram 通知文本。"""

    @staticmethod
    def build(job, status, task_num, duration_text, size_text) -> tuple:
        status_name = {
            2: "成功", 3: "完成（部分失败）", 4: "已中止", 6: "失败", 7: "失败",
        }.get(status, str(status))
        title = "同步任务{}".format(status_name)
        if job.remark:
            title = "{}: {}".format(job.remark, status_name)
        content = "源：{}\n目标：{}\n总数：{} 成功：{} 失败：{}".format(
            job.src_path, job.dst_path.replace(":", "、"),
            task_num.get("allNum", 0), task_num.get("successNum", 0),
            task_num.get("failNum", 0),
        )
        if duration_text is not None:
            content += "\n耗时：{} 大小：{}".format(duration_text, size_text)
        return title, content


def notify_task(job, status, task_num, duration_text, size_text):
    """通知钩子：装配到 JobClient.notify_hook。"""
    svc = getattr(job, "_notify_svc", None)
    if svc is None:
        return
    notifier = getattr(svc, "notifier", None)
    if notifier is None:
        return
    try:
        title, content = SyncNotifyAdapter.build(job, status, task_num, duration_text, size_text)
        notifier.send(title, content)
    except Exception:
        logger.exception("同步通知失败")


def init_jobs(session, services=None) -> None:
    """启动时：修正异常中止的任务状态，并为每个启用作业重建调度。"""
    from core.sync.job_dao import update_job_task_status_by_status

    update_job_task_status_by_status(session)
    sf = getattr(services, "session_factory", None) if services else None
    for item in get_job_list(session):
        try:
            # 重建已存在作业的 JobClient 并恢复调度（get_job_client_by_id 从 ORM
            # 构建，不会重复插入；add_job_client 期望 dict 且会再插一条，不可用）。
            get_job_client_by_id(session, item.id, services, session_factory=sf)
        except Exception:
            logger.exception("启动添加作业 %s 失败", getattr(item, "id", "?"))


def get_job_client_by_id(session, job_id: int, services=None, session_factory=None) -> JobClient:
    job_id = int(job_id)
    if job_id in job_client_list:
        return job_client_list[job_id]
    job = get_job_by_id(session, job_id)
    sf = session_factory or (getattr(services, "session_factory", None) if services else None)
    client = JobClient(job, session, session_factory=sf)
    if services is not None:
        client.notify_hook = lambda j, st, tn, dt, sz: _notify_with_services(
            services, j, st, tn, dt, sz
        )
    job_client_list[job_id] = client
    return client


def _notify_with_services(services, job, status, task_num, duration_text, size_text):
    notifier = getattr(services, "notifier", None)
    if notifier is None:
        return
    try:
        title, content = SyncNotifyAdapter.build(job, status, task_num, duration_text, size_text)
        notifier.send(title, content)
    except Exception:
        logger.exception("同步通知失败")


def clean_job_input(job: dict):
    if job.get("isCron") == 2 and job.get("enable") != 1:
        job["enable"] = 1
    for key, value in list(job.items()):
        if isinstance(value, str):
            if value.strip() == "":
                job[key] = None
            else:
                job[key] = value.strip()
    if job.get("exclude") is not None:
        job["exclude"] = ":".join(item.strip() for item in job["exclude"].split(":"))
    job.setdefault("minFileSize", None)
    job.setdefault("maxFileSize", None)
    job.setdefault("sourceMode", 0)
    job["minFileSize"] = normalize_file_size(job["minFileSize"])
    job["maxFileSize"] = normalize_file_size(job["maxFileSize"])
    job["sourceMode"] = normalize_source_mode(job["sourceMode"])
    if job.get("srcPath") and job.get("dstPath"):
        for dst_path in job["dstPath"].split(":"):
            engine_id = job.get("engineId")
            paths_overlap = (
                virtual_paths_overlap(job["srcPath"], dst_path)
                if engine_id is None
                else storage_engine.engine_mounts_overlap
            )
            if engine_id is not None:
                from core.sync import engine as se
                if se.engine_mounts_overlap(_session_for(), engine_id, job["srcPath"], dst_path):
                    raise ValueError("源路径与目标路径存在重叠，无法同步")
            elif virtual_paths_overlap(job["srcPath"], dst_path):
                raise ValueError("源路径与目标路径存在重叠，无法同步")
    if (job["minFileSize"] is not None
            and job["maxFileSize"] is not None
            and job["minFileSize"] > job["maxFileSize"]):
        raise ValueError("最小文件大小不能大于最大文件大小")


_session_holder = {"session": None}


def _session_for():
    return _session_holder["session"]


def add_job_client(job: dict, session, is_init=False, services=None, session_factory=None):
    from core.sync.job_dao import add_job, get_job_by_id

    set_session(session)
    clean_job_input(job)
    job_id = add_job(session, job)
    orm_job = get_job_by_id(session, job_id)
    sf = session_factory or (getattr(services, "session_factory", None) if services else None)
    client = JobClient(orm_job, session, is_init=is_init, session_factory=sf)
    job_client_list[int(client.job_id)] = client
    if services is not None:
        client.notify_hook = lambda j, st, tn, dt, sz: _notify_with_services(
            services, j, st, tn, dt, sz
        )
    return client


def edit_job_client(job: dict, session, services=None, session_factory=None):
    job_id = int(job["id"])
    set_session(session)
    clean_job_input(job)
    sf = session_factory or (getattr(services, "session_factory", None) if services else None)
    client = get_job_client_by_id(session, job_id, services, session_factory=sf)
    if client.job.enable == 1 and client.job.is_cron != 2:
        raise ValueError("请先禁用作业再编辑")
    clear_snapshot = any(
        getattr(client.job, key, None) != job.get(key) for key in SOURCE_SNAPSHOT_FIELDS
    )
    old_job = client.job
    client.stop_job(remove=True)
    new_client = None
    try:
        new_job_row = get_job_by_id(session, job_id)
        for k, v in job.items():
            if k == "id":
                continue
            setattr(new_job_row, k, v)
        session.flush()
        new_client = JobClient(new_job_row, session, session_factory=sf)
        if services is not None:
            new_client.notify_hook = lambda j, st, tn, dt, sz: _notify_with_services(
                services, j, st, tn, dt, sz
            )
    except Exception:
        if new_client is not None:
            try:
                new_client.stop_job(remove=True)
            except Exception:
                pass
        try:
            restored = get_job_by_id(session, job_id)
            job_client_list[job_id] = JobClient(restored, session, session_factory=sf)
        except Exception:
            logger.exception("恢复作业失败")
        raise
    job_client_list[job_id] = new_client


def do_all_job_manual(session, services=None):
    job_list = get_enable_job_list(session)
    if not job_list:
        raise ValueError("没有可手动运行的作业")
    sf = getattr(services, "session_factory", None) if services else None
    for job_item in job_list:
        client = get_job_client_by_id(session, job_item.id, services, session_factory=sf)
        if client.job.enable == 1:
            client.do_manual(session_factory=sf)


def pause_all_job(session):
    for job in get_job_list(session):
        if job.is_cron == 2 or job.enable != 1:
            continue
        pause_job(session, job.id)


def continue_all_job(session):
    for job in get_job_list(session):
        if job.is_cron == 2 or job.enable == 1:
            continue
        continue_job(session, job.id)


def do_job_manual(job_id: int, session, services=None, operator="手动"):
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    if client.job.enable != 1:
        raise ValueError("已禁用的作业不能运行")
    client.do_manual(operator=operator, session_factory=sf)


def remove_job_client(job_id: int, session):
    job_id = int(job_id)
    client = get_job_client_by_id(session, job_id)
    client.stop_job(remove=True)
    delete_job(session, job_id)
    job_client_list.pop(job_id, None)


def continue_job(job_id: int, session, services=None):
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    client.resume_job()


def pause_job(job_id: int, session, services=None):
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    if client.job.is_cron == 2:
        raise ValueError("手动作业不能禁用")
    client.stop_job()


def abort_job(job_id: int, session, services=None):
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    client.abort_job()


def get_job_list_view(session, req=None):
    result = get_job_list(session, req)
    rows = result.get("dataList", result) if isinstance(result, dict) else result
    latest_tasks = {
        task["jobId"]: task for task in get_latest_job_task_list(
            session, [row.id for row in rows]
        )
    }
    for row in rows:
        task = latest_tasks.get(row.id)
        if task is None:
            row.last_task = None
            continue
        task_num = {}
        if task.get("taskNum"):
            try:
                parsed = json.loads(task["taskNum"])
                if isinstance(parsed, dict):
                    task_num = parsed
            except (TypeError, ValueError):
                pass
        task.all_num = task_num.get("allNum", task.get("itemAllNum") or 0)
        task.success_num = task_num.get("successNum", task.get("itemSuccessNum") or 0)
        task.fail_num = task_num.get("failNum", task.get("itemFailNum") or 0)
        task.other_num = task_num.get("otherNum", task.get("itemOtherNum") or 0)
        task.duration = task_num.get("duration")
        row.last_task = task
    return result


def get_job_current(job_id: int, session, status=None, services=None):
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    task_client = client.current_job_task
    if task_client is not None:
        if status is None:
            return task_client.get_current()
        return task_client.get_current_by_status(int(status))
    return None


def get_job_progress(job_id: int, session, services=None):
    """聚合同步作业实时进度（持久化中间表），供 Web/CLI 展示与中断恢复。

    恢复基数：上次运行中已成功文件数（跨运行累计完成度）。
    """
    sf = getattr(services, "session_factory", None) if services else None
    client = get_job_client_by_id(session, int(job_id), services, session_factory=sf)
    current = client.current_job_task
    current_task_id = current.task_id if current is not None else 0
    recovered = 0
    if current_task_id:
        from core.sync.job_dao import count_progress_recovered

        recovered = count_progress_recovered(session, int(job_id), current_task_id)
    # 传入请求线程自己的 session，避免跨线程复用后台线程私有的 self.session
    # （会触发 "Instance is not bound to a Session"）。
    return client.get_progress(recovered_base=recovered, session=session)


def set_session(session) -> None:
    """供 clean_job_input 内重叠检测临时取用（不推荐并发使用）。"""
    _session_holder["session"] = session
