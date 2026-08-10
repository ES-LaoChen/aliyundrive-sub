"""同步运行记录蓝图（sync_records 审计 / 导出 / 过滤）。"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, render_template, request

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("sync_records_bp", __name__, url_prefix="/sync-records")


def _services() -> Services:
    return current_app.config["SERVICES"]


def _sync():
    svc = _services()
    if svc.sync_service is None:
        raise RuntimeError("同步管理模块未初始化")
    return svc.sync_service


@bp.route("/", methods=["GET"], strict_slashes=False)
def index():
    sync = _sync()
    job_id = request.args.get("jobId")
    page_size = int(request.args.get("pageSize", 20))
    page_num = int(request.args.get("pageNum", 1))
    try:
        records = sync.get_record_list(
            int(job_id) if job_id else None, page_size=page_size, page_num=page_num)
    except Exception as e:
        logger.exception("读取同步记录失败")
        records = {"dataList": [], "total": 0}
    return render_template("sync_records.html", records=records, job_id=job_id)
