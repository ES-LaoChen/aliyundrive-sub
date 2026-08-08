"""同步管理蓝图：作业列表 / 新增 / 编辑 / 删除 / 手动运行 / 暂停 / 继续 / 中止。

仅移动模式（method=2）。调度支持间隔 / cron / 手动三种。
存储目录（挂载）的管理入口在「系统设置」蓝图（engine 管理区）。
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from core.sync.job_dao import get_job_by_id
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("sync", __name__, url_prefix="/sync")

# 调度类型：0-间隔 1-cron 2-手动。
SCHED_TYPES = (
    {"value": 0, "label": "间隔"},
    {"value": 1, "label": "Cron"},
    {"value": 2, "label": "手动"},
)


def _services() -> Services:
    return current_app.config["SERVICES"]


def _mounts():
    """返回挂载列表，供表单提示路径前缀。"""
    svc = _services()
    try:
        return svc.sync_service.get_mount_list()
    except Exception:
        logger.exception("读取挂载列表失败")
        return []


@bp.route("/", methods=["GET"], strict_slashes=False)
def index():
    svc = _services()
    sched_filter = request.args.get("sched", "all")
    try:
        view = svc.sync_service.get_job_list_view()
        jobs = view.get("dataList", view) if isinstance(view, dict) else view
    except Exception:
        logger.exception("读取同步作业失败")
        jobs = []
    if sched_filter in ("0", "1", "2"):
        jobs = [j for j in jobs if str(getattr(j, "isCron", "")) == sched_filter]
    else:
        sched_filter = "all"
    mounts = _mounts()
    return render_template(
        "sync_jobs.html", jobs=jobs, sched_filter=sched_filter,
        sched_types=SCHED_TYPES, mounts=mounts,
    )


@bp.route("/new", methods=["GET"])
def new():
    svc = _services()
    mounts = _mounts()
    engine_id = 0
    try:
        engine_id = svc.sync_service.get_system_engine_id()
    except Exception:
        logger.exception("读取系统引擎失败")
    form = {
        "remark": "", "engineId": engine_id, "srcPath": "", "dstPath": "",
        "isCron": 0, "interval": 3600, "sourceMode": 0,
        "exclude": "", "minFileSize": "", "maxFileSize": "", "enable": 1,
    }
    return render_template(
        "sync_job_edit.html", job=None, form=form,
        sched_types=SCHED_TYPES, mounts=mounts,
    )


@bp.route("", methods=["POST"])
def create():
    svc = _services()
    job = _collect_form()
    try:
        svc.sync_service.add_job(job)
    except Exception as exc:
        logger.exception("新增同步作业失败")
        flash(f"保存失败：{exc}", "error")
        return render_template(
            "sync_job_edit.html", job=None, form=job,
            sched_types=SCHED_TYPES, mounts=_mounts(),
        )
    flash(f"同步作业「{job.get('remark') or ''}」已创建", "success")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/edit", methods=["GET"])
def edit(job_id: int):
    svc = _services()
    with svc.session_factory() as db:
        job = get_job_by_id(db, job_id)
    if job is None:
        flash("作业不存在", "error")
        return redirect(url_for("sync.index"))
    form = {k: v for k, v in job.__dict__.items() if not k.startswith("_")}
    # 数值字段友好化：size 空字符串。
    for k in ("minFileSize", "maxFileSize"):
        form[k] = "" if form.get(k) is None else form[k]
    return render_template(
        "sync_job_edit.html", job=job, form=form,
        sched_types=SCHED_TYPES, mounts=_mounts(),
    )


@bp.route("/<int:job_id>/edit", methods=["POST"])
def update(job_id: int):
    svc = _services()
    job = _collect_form()
    job["id"] = job_id
    try:
        svc.sync_service.edit_job(job)
    except Exception as exc:
        logger.exception("编辑同步作业失败")
        flash(f"保存失败：{exc}", "error")
        with svc.session_factory() as db:
            row = get_job_by_id(db, job_id)
        form = {k: v for k, v in (row or job).__dict__.items() if not k.startswith("_")}
        return render_template(
            "sync_job_edit.html", job=row, form=form,
            sched_types=SCHED_TYPES, mounts=_mounts(),
        )
    flash(f"同步作业「{job.get('remark') or ''}」已保存", "success")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/delete", methods=["POST"])
def delete(job_id: int):
    svc = _services()
    try:
        svc.sync_service.remove_job(job_id)
    except Exception as exc:
        logger.exception("删除同步作业失败")
        flash(f"删除失败：{exc}", "error")
        return redirect(url_for("sync.index"))
    flash("同步作业已删除", "success")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/run", methods=["POST"])
def run(job_id: int):
    svc = _services()
    try:
        svc.sync_service.do_job_manual(job_id)
        flash(f"已触发作业 #{job_id} 手动运行", "info")
    except Exception as exc:
        logger.exception("手动运行同步作业失败")
        flash(f"运行失败：{exc}", "error")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/pause", methods=["POST"])
def pause(job_id: int):
    svc = _services()
    try:
        svc.sync_service.pause_job(job_id)
        flash(f"作业 #{job_id} 已禁用", "info")
    except Exception as exc:
        logger.exception("禁用同步作业失败")
        flash(f"禁用失败：{exc}", "error")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/resume", methods=["POST"])
def resume(job_id: int):
    svc = _services()
    try:
        svc.sync_service.continue_job(job_id)
        flash(f"作业 #{job_id} 已启用", "info")
    except Exception as exc:
        logger.exception("启用同步作业失败")
        flash(f"启用失败：{exc}", "error")
    return redirect(url_for("sync.index"))


@bp.route("/<int:job_id>/abort", methods=["POST"])
def abort(job_id: int):
    svc = _services()
    try:
        svc.sync_service.abort_job(job_id)
        flash(f"作业 #{job_id} 已中止", "info")
    except Exception as exc:
        logger.exception("中止同步作业失败")
        flash(f"中止失败：{exc}", "error")
    return redirect(url_for("sync.index"))


@bp.route("/run-all", methods=["POST"])
def run_all():
    svc = _services()
    try:
        svc.sync_service.do_all_manual()
        flash("已触发全部启用作业手动运行", "info")
    except Exception as exc:
        logger.exception("批量运行失败")
        flash(f"运行失败：{exc}", "error")
    return redirect(url_for("sync.index"))


@bp.route("/current", methods=["GET"])
def current():
    """返回作业当前运行状态（JSON），供前端轮询。"""
    svc = _services()
    job_id = request.args.get("job_id", type=int)
    if job_id is None:
        return jsonify(error="job_id required"), 400
    try:
        data = svc.sync_service.get_job_current(job_id)
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(data)


def _session(svc):
    # 复用 sync_service 的 session_factory 取一次性会话。
    holder = {"db": None}

    class _Ctx:
        def __enter__(self_inner):
            holder["db"] = svc.session_factory()()
            return holder["db"]

        def __exit__(self_inner, *exc):
            holder["db"].close()

    with _Ctx() as db:
        return db


def _collect_form() -> dict:
    """从 request.form 收集同步作业字段，统一成 job dict。"""
    form = request.form
    job = {
        "remark": (form.get("remark") or "").strip(),
        "engineId": int(form.get("engineId") or 0),
        "srcPath": (form.get("srcPath") or "").strip(),
        "dstPath": (form.get("dstPath") or "").strip(),
        # 仅移动模式。
        "method": 2,
        "sourceMode": int(form.get("sourceMode") or 0),
        "isCron": int(form.get("isCron") or 0),
        "interval": int(form.get("interval") or 0),
        "exclude": (form.get("exclude") or "").strip(),
        "enable": 1 if form.get("enable") == "1" else 0,
        # cron 字段
        "year": (form.get("year") or "").strip(),
        "month": (form.get("month") or "").strip(),
        "day": (form.get("day") or "").strip(),
        "week": (form.get("week") or "").strip(),
        "day_of_week": (form.get("day_of_week") or "").strip(),
        "hour": (form.get("hour") or "").strip(),
        "minute": (form.get("minute") or "").strip(),
        "second": (form.get("second") or "").strip(),
        "start_date": (form.get("start_date") or "").strip(),
        "end_date": (form.get("end_date") or "").strip(),
    }
    min_fs = (form.get("minFileSize") or "").strip()
    max_fs = (form.get("maxFileSize") or "").strip()
    job["minFileSize"] = int(min_fs) if min_fs else None
    job["maxFileSize"] = int(max_fs) if max_fs else None
    return job
