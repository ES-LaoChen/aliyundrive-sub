"""任务完成后的状态更新与通知（移植自 TaoSync service/syncJob/taskService.py）。

通知改为复用当前项目的 ``NotifierManager.send(title, content, level)``，不再使用
TaoSync 的 notifyService（DingTalk/ServerChan）。文案直接以中文内联，保持可诊断性。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from core.sync import job_dao

logger = logging.getLogger(__name__)


def _convert_seconds(seconds):
    if not seconds or seconds < 0:
        return 0, 0, 0
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return hours, minutes, seconds


def _convert_bytes(num):
    try:
        n = int(num or 0)
    except (TypeError, ValueError):
        return str(num)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def update_job_task_status(task_id, status, err_msg=None, task_list=None,
                           create_time=None, notifier=None, session_factory=None):
    duration = int(time.time() - create_time) if create_time else None
    job_dao.update_job_task_status(task_id, status, err_msg, session_factory=session_factory)
    job = job_dao.get_job_by_task_id(task_id, session_factory=session_factory)

    if task_list is not None:
        hours, minutes, seconds = _convert_seconds(duration)
        duration_text = f"{hours}小时{minutes}分{seconds}秒"
        sum_size = sum(item['fileSize'] for item in task_list[2]
                       if item.get('fileSize') is not None)
        size_text = _convert_bytes(sum_size)
        context_ext = f"（耗时 {duration_text}，同步大小 {size_text}）"
        task_num = {
            'waitNum': 0,
            'runningNum': 0,
            'successNum': len(task_list[2]),
            'failNum': len(task_list[7]),
            'otherNum': len(task_list[-1]),
            'allNum': len(task_list[2]) + len(task_list[7]) + len(task_list[-1]),
            'duration': duration,
            'sumSize': sum_size,
        }
    else:
        task_num = get_cu_task_num(task_id, session_factory=session_factory)

    job_dao.update_job_task_num_many(
        [{'taskId': task_id, 'taskNum': json.dumps(task_num)}],
        session_factory=session_factory,
    )

    status_name = {
        0: "等待中", 1: "运行中", 2: "成功", 3: "部分失败", 4: "已中止",
        5: "超时", 6: "失败", 7: "失败",
    }.get(status, f"状态{status}")

    if notifier is not None:
        need_not_sync = False
        if status == 2 and task_num['allNum'] == 0:
            need_not_sync = True
            status_name = "无需同步"
        title = f"同步任务 {status_name}"
        content = (
            f"作业：{job.get('remark') or job.get('id')}\n"
            f"来源：{job.get('srcPath')}\n"
            f"目标：{job.get('dstPath', '').replace(':', '、')}\n"
            f"总计：{task_num['allNum']} 成功：{task_num['successNum']} "
            f"失败：{task_num['failNum']}"
        )
        if create_time is not None:
            content = content + context_ext
        if 3 < status < 6 or status == 7:
            content += f"\n状态：{status_name}"
        elif status == 6 and err_msg is not None:
            content += f"\n错误：{err_msg}"
        try:
            notifier.send(title, content, "warning" if status != 2 else "info")
        except Exception as e:
            logger.error("同步通知发送失败：%s", e)


def get_task_list(req, session_factory=None):
    job_id = req.get('id')
    page_size = req.get('pageSize')
    page_num = req.get('pageNum')
    return job_dao.get_job_task_list(
        job_id, page_size=page_size, page_num=page_num, session_factory=session_factory)


def get_cu_task_num(task_id, session_factory=None):
    return {
        'waitNum': job_dao.get_job_task_count_by_status(task_id, 0, session_factory),
        'runningNum': job_dao.get_job_task_count_by_status(task_id, 1, session_factory),
        'successNum': job_dao.get_job_task_count_by_status(task_id, 2, session_factory),
        'failNum': job_dao.get_job_task_count_by_status(task_id, 7, session_factory),
        'otherNum': job_dao.get_job_task_count_by_other(task_id, session_factory),
        'allNum': job_dao.get_job_task_count_by_all(task_id, session_factory),
    }


def remove_task(task_id, session_factory=None):
    job_dao.delete_job_task_by_task_id(task_id, session_factory=session_factory)


def get_task_item_list(req, session_factory=None):
    return job_dao.get_job_task_item_list(
        req.get('taskId'), status=req.get('status'), item_type=req.get('type'),
        page_size=req.get('pageSize'), page_num=req.get('pageNum'),
        session_factory=session_factory,
    )
