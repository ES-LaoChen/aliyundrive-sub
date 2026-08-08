"""同步记录蓝图：同步操作历史日志的查询、筛选与导出。

记录由同步引擎在每次运行结束时写入（core/sync/job_client.py 的
JobTask._write_sync_record），本蓝图仅负责展示与导出。
"""
from __future__ import annotations

import csv
import io
import logging

from flask import (
    Blueprint,
    Response,
    current_app,
    render_template,
    request,
    url_for,
)

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("sync_records", __name__, url_prefix="/sync/records")

# status -> 中文标签（与 sync_tasks.status 对齐：2-成功 3-部分失败 4-中止 6-失败 7-其他）。
STATUS_LABELS = {
    2: "成功",
    3: "部分失败",
    4: "中止",
    6: "失败",
    7: "其他",
}


def _services() -> Services:
    return current_app.config["SERVICES"]


def _parse_dt(value: str):
    """把 'YYYY-MM-DD HH:MM:SS' 解析为 Unix 秒；非法返回 None。"""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            from datetime import datetime
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    return None


def _status_label(status: int) -> str:
    try:
        return STATUS_LABELS.get(int(status), f"状态{status}")
    except (TypeError, ValueError):
        return "未知"


@bp.route("", methods=["GET"])
def index():
    svc = _services()
    params = {
        "jobId": request.args.get("jobId", "").strip(),
        "status": request.args.get("status", "").strip(),
        "operator": request.args.get("operator", "").strip(),
        "startTimeFrom": _parse_dt(request.args.get("startTimeFrom", "")),
        "startTimeTo": _parse_dt(request.args.get("startTimeTo", "")),
        "pageNum": request.args.get("pageNum", 1),
        "pageSize": request.args.get("pageSize", 20),
    }
    try:
        view = svc.sync_service.get_sync_record_list(params)
        rows = view.get("dataList", [])
        total = view.get("total", 0)
        page_num = view.get("pageNum", 1)
        page_size = view.get("pageSize", 20)
    except Exception:
        logger.exception("读取同步记录失败")
        rows, total, page_num, page_size = [], 0, 1, 20

    # 装饰：附加状态标签与时间文本。
    for r in rows:
        r.status_label = _status_label(r.status)
        r.start_text = _ts(r.startTime)
        r.end_text = _ts(r.endTime)

    # 回显用：把解析后的 Unix 还原为输入框文本（解析失败则留空）。
    display_filters = {
        "jobId": request.args.get("jobId", "").strip(),
        "status": request.args.get("status", "").strip(),
        "operator": request.args.get("operator", "").strip(),
        "startTimeFrom": request.args.get("startTimeFrom", "").strip(),
        "startTimeTo": request.args.get("startTimeTo", "").strip(),
    }
    return render_template(
        "sync_records.html",
        records=rows, total=total, page_num=page_num, page_size=page_size,
        status_labels=STATUS_LABELS,
        filters=display_filters,
    )


@bp.route("/export", methods=["GET"])
def export_csv():
    svc = _services()
    params = {
        "jobId": request.args.get("jobId", "").strip(),
        "status": request.args.get("status", "").strip(),
        "operator": request.args.get("operator", "").strip(),
        "startTimeFrom": _parse_dt(request.args.get("startTimeFrom", "")),
        "startTimeTo": _parse_dt(request.args.get("startTimeTo", "")),
    }
    try:
        rows = svc.sync_service.get_all_sync_records(params)
    except Exception:
        logger.exception("导出同步记录失败")
        rows = []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "记录ID", "作业ID", "作业名称", "操作人员", "状态", "同步文件数",
        "同步数据量(字节)", "错误信息", "开始时间", "结束时间", "记录时间",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.jobId, r.jobName, r.operator, _status_label(r.status),
            r.dataCount, r.dataSize, r.errMsg,
            _ts(r.startTime), _ts(r.endTime), _ts(r.createTime),
        ])
    csv_data = "\ufeff" + buf.getvalue()  # BOM 便于 Excel 识别 UTF-8
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sync_records.csv"},
    )


def _ts(value: int) -> str:
    """Unix 秒 -> 本地时间文本（%Y-%m-%d %H:%M:%S），非法值返回空串。"""
    try:
        from datetime import datetime
        if not value:
            return ""
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""
