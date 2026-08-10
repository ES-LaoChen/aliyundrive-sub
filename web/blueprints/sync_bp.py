"""同步作业蓝图：作业列表 / 新建 / 编辑 / 删除 / 手动执行 / 启停 / 进度。"""
from __future__ import annotations

import logging
import json

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("sync_bp", __name__, url_prefix="/sync")


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
    try:
        jobs = sync.get_job_list({})
    except Exception as e:
        logger.exception("读取同步作业列表失败")
        jobs = []
        flash("读取同步作业列表失败：{}".format(e), "error")
    return render_template("sync_jobs.html", jobs=jobs,
                           mounts_exist=sync.validate_mounts_exist())


@bp.route("/new", methods=["GET"])
def new():
    sync = _sync()
    engines = sync.get_engine_list()
    supported = sync.get_supported_drivers()
    return render_template("sync_job_edit.html", job=None, engines=engines,
                           supported=supported, form={})


@bp.route("", methods=["POST"])
def create():
    sync = _sync()
    form = dict(request.form.to_dict())
    job = _form_to_job(form)
    try:
        job_id = sync.add_job(job)
    except Exception as e:
        logger.exception("创建同步作业失败")
        flash("创建失败：{}".format(e), "error")
        engines = sync.get_engine_list()
        supported = sync.get_supported_drivers()
        return render_template("sync_job_edit.html", job=None, engines=engines,
                               supported=supported, form=form)
    flash("同步作业已创建", "success")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/edit", methods=["GET"])
def edit(job_id: int):
    sync = _sync()
    try:
        job = sync.get_job_by_id(job_id)
    except Exception:
        flash("作业不存在", "error")
        return redirect(url_for("sync_bp.index"))
    engines = sync.get_engine_list()
    supported = sync.get_supported_drivers()
    return render_template("sync_job_edit.html", job=job, engines=engines,
                           supported=supported, form=job)


@bp.route("/<int:job_id>/edit", methods=["POST"])
def update(job_id: int):
    sync = _sync()
    form = dict(request.form.to_dict())
    job = _form_to_job(form)
    job["id"] = job_id
    try:
        sync.update_job(job)
    except Exception as e:
        logger.exception("编辑同步作业失败")
        flash("编辑失败：{}".format(e), "error")
        engines = sync.get_engine_list()
        supported = sync.get_supported_drivers()
        form["id"] = job_id
        return render_template("sync_job_edit.html", job=job, engines=engines,
                               supported=supported, form=form)
    flash("同步作业已更新", "success")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/delete", methods=["POST"])
def delete(job_id: int):
    sync = _sync()
    try:
        sync.remove_job(job_id)
        flash("同步作业已删除", "success")
    except Exception as e:
        logger.exception("删除同步作业失败")
        flash("删除失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/run", methods=["POST"])
def run(job_id: int):
    sync = _sync()
    try:
        sync.do_job_manual(job_id)
        flash("已触发手动同步", "success")
    except Exception as e:
        logger.exception("手动同步失败")
        flash("手动同步失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/pause", methods=["POST"])
def pause(job_id: int):
    sync = _sync()
    try:
        sync.pause_job(job_id)
        flash("已禁用作业", "success")
    except Exception as e:
        flash("禁用失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/resume", methods=["POST"])
def resume(job_id: int):
    sync = _sync()
    try:
        sync.continue_job(job_id)
        flash("已启用作业", "success")
    except Exception as e:
        flash("启用失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/<int:job_id>/abort", methods=["POST"])
def abort(job_id: int):
    sync = _sync()
    try:
        sync.abort_job(job_id)
        flash("已发送中止信号", "success")
    except Exception as e:
        flash("中止失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/run-all", methods=["POST"])
def run_all():
    sync = _sync()
    try:
        sync.do_all_job_manual()
        flash("已触发所有启用作业", "success")
    except Exception as e:
        flash("触发失败：{}".format(e), "error")
    return redirect(url_for("sync_bp.index"))


@bp.route("/progress/<int:job_id>", methods=["GET"])
def progress(job_id: int):
    sync = _sync()
    try:
        data = sync.get_job_current(job_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # 前端轮询（Accept: application/json 或 ?format=json）返回 JSON；
    # 普通页面访问渲染实时进度仪表盘。
    wants_json = request.args.get("format") == "json" or \
        request.headers.get("Accept", "").startswith("application/json")
    if wants_json:
        return jsonify(data or {"scanFinish": False, "doingTask": []})
    return render_template("sync_progress_dashboard.html", job_id=job_id,
                           initial=data or {"scanFinish": False, "doingTask": []})


@bp.route("/tasks/<int:job_id>", methods=["GET"])
def tasks(job_id: int):
    sync = _sync()
    page_size = int(request.args.get("pageSize", 10))
    page_num = int(request.args.get("pageNum", 1))
    try:
        tasks = sync.get_task_list({"id": job_id, "pageSize": page_size, "pageNum": page_num})
    except Exception as e:
        logger.exception("读取任务列表失败")
        tasks = {"dataList": [], "total": 0}
        flash("读取任务列表失败：{}".format(e), "error")
    # 把 taskNum（JSON 字符串）解析为 dict，便于模板读取 successNum/failNum。
    if isinstance(tasks, dict):
        for t in tasks.get("dataList", []):
            raw = t.get("taskNum")
            if isinstance(raw, str) and raw.strip():
                try:
                    t["taskNum"] = json.loads(raw)
                except (TypeError, ValueError):
                    t["taskNum"] = {}
            elif raw is None:
                t["taskNum"] = {}
    return render_template("sync_progress.html", job_id=job_id, tasks=tasks)


@bp.route("/tasks/<int:job_id>/<int:task_id>/items", methods=["GET"])
def task_items(job_id: int, task_id: int):
    sync = _sync()
    page_size = int(request.args.get("pageSize", 50))
    page_num = int(request.args.get("pageNum", 1))
    try:
        items = sync.get_task_item_list(
            {"taskId": task_id, "pageSize": page_size, "pageNum": page_num})
    except Exception as e:
        logger.exception("读取任务条目失败")
        items = {"dataList": [], "total": 0}
    return render_template("sync_task_items.html", job_id=job_id,
                           task_id=task_id, items=items)


def _form_to_job(form):
    def _int(v, default=0):
        v = (v or "").strip()
        if v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    alist_id = form.get("alistId", "").strip()
    return {
        "enable": 1 if form.get("enable") == "on" or form.get("enable") == "1" else 0,
        "remark": form.get("remark", "").strip(),
        "srcPath": form.get("srcPath", "").strip(),
        "dstPath": form.get("dstPath", "").strip(),
        "alistId": int(alist_id) if alist_id else None,
        "useCacheT": _int(form.get("useCacheT", "1"), 1),
        "scanIntervalT": _int(form.get("scanIntervalT", "0"), 0),
        "useCacheS": _int(form.get("useCacheS", "1"), 1),
        "scanIntervalS": _int(form.get("scanIntervalS", "0"), 0),
        "method": _int(form.get("method", "0"), 0),
        "sourceMode": _int(form.get("sourceMode", "0"), 0),
        "interval": _int(form.get("interval", "0"), 0),
        "isCron": _int(form.get("isCron", "0"), 0),
        "year": form.get("year", "").strip(),
        "month": form.get("month", "").strip(),
        "day": form.get("day", "").strip(),
        "week": form.get("week", "").strip(),
        "day_of_week": form.get("day_of_week", "").strip(),
        "hour": form.get("hour", "").strip(),
        "minute": form.get("minute", "").strip(),
        "second": form.get("second", "").strip(),
        "start_date": form.get("start_date", "").strip(),
        "end_date": form.get("end_date", "").strip(),
        "exclude": form.get("exclude", "").strip(),
        "minFileSize": form.get("minFileSize", "").strip() or None,
        "maxFileSize": form.get("maxFileSize", "").strip() or None,
    }
