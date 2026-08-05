"""订阅详情蓝图（T-D5，PRD §4.1 / DESIGN §4.1）。

只读路由 ``GET /subscriptions/<int:sub_id>/detail``：
- 头部：订阅基本信息（share_url / target_folder_path / interval / status / last_check_at）
- 区块 1：运行历史（runs 最近 20 条）
- 区块 2：转存任务（transfer_tasks 最近 50 条）

不触发任何 check，纯只读查询；失败详情用 HTML ``<details>`` 折叠展开，避免引入
JS 依赖。
"""
from __future__ import annotations

import json
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

from models import Run, Subscription, TransferTask
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("subscription_detail", __name__, url_prefix="/subscriptions")


def _services() -> Services:
    return current_app.config["SERVICES"]


@bp.route("/<int:sub_id>/clear-records", methods=["POST"])
def clear_records(sub_id: int):
    """清空订阅某类历史记录列表（运行记录 / 转存记录）。

    仅删除 ``subscription_id == sub_id`` 且类型为 ``kind`` 的记录，不影响其他列表。
    按 ``kind`` 区分：``runs`` -> Run（运行记录），``tasks`` -> TransferTask（转存记录）。
    """
    svc = _services()
    payload = request.get_json(silent=True) or {}
    kind = payload.get("kind") or request.form.get("kind")
    if kind not in ("runs", "tasks"):
        return jsonify({"error": "无效的记录类型"}), 400

    model = Run if kind == "runs" else TransferTask
    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            return jsonify({"error": "订阅不存在"}), 404
        try:
            deleted = db.query(model).filter(
                model.subscription_id == sub_id
            ).delete(synchronize_session=False)
            db.commit()
        except Exception as exc:  # pragma: no cover - 防御性兜底
            db.rollback()
            logger.exception("清理%s记录失败 sub=%s", kind, sub_id)
            return jsonify({"error": f"清理失败：{exc}"}), 500
    logger.info("已清空订阅 %s 的%s记录 %s 条", sub_id, kind, deleted)
    return jsonify({"deleted": int(deleted)}), 200


@bp.route("/<int:sub_id>/detail", methods=["GET"])
def detail(sub_id: int):
    svc = _services()
    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            flash("订阅不存在", "error")
            return redirect(url_for("subscriptions.index"))
        # 运行历史（最近 20 条）
        runs = (
            db.query(Run)
            .filter(Run.subscription_id == sub_id)
            .order_by(Run.started_at.desc())
            .limit(20)
            .all()
        )
        # 转存任务（最近 50 条）
        tasks = (
            db.query(TransferTask)
            .filter(TransferTask.subscription_id == sub_id)
            .order_by(TransferTask.updated_at.desc())
            .limit(50)
            .all()
        )

    # 解析 run summary JSON
    runs_view = []
    for r in runs:
        try:
            summary = json.loads(r.summary) if r.summary else {}
        except (ValueError, TypeError):
            summary = {}
        runs_view.append({
            "id": r.id,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "status": r.status,
            "run_mode": r.run_mode,
            "summary": summary,
        })

    return render_template(
        "subscription_detail.html",
        sub=sub,
        runs=runs_view,
        tasks=tasks,
    )
