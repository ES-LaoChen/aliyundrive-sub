"""转存记录蓝图：按结果 / 订阅筛选查看，并支持记录清理。

路由：
- ``GET /``（``index``）：按 ``status`` / ``subscription_id`` 筛选 ``TransferRecord``。
- ``POST /clean``（``clean``）：批量删除选中记录，或按时间范围（7d / 30d / all）清理。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import List, Tuple

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

from db import utc_now
from models import Subscription, TransferRecord
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("logs", __name__, url_prefix="/logs")


def _services() -> Services:
    """从 Flask app 配置取出 ``Services`` 容器。"""
    return current_app.config["SERVICES"]


@bp.route("", methods=["GET"])
def index():
    svc = _services()
    status = request.args.get("status", "")
    sub_id = request.args.get("subscription_id", "")
    with svc.session_factory() as db:
        q = db.query(TransferRecord)
        if status:
            q = q.filter(TransferRecord.status == status)
        if sub_id:
            try:
                q = q.filter(TransferRecord.subscription_id == int(sub_id))
            except ValueError:
                pass
        records = q.order_by(TransferRecord.id.desc()).limit(500).all()
        subs = db.query(Subscription).order_by(Subscription.id).all()
    return render_template(
        "logs.html",
        records=records,
        subscriptions=subs,
        status=status,
        sub_id=str(sub_id),
    )


def _parse_clean_input() -> Tuple[List[int], str]:
    """解析 ``/clean`` 的入参（优先 JSON，否则回退到表单）。

    Returns:
        ``(valid_ids, mode)``：
        - ``valid_ids``：通过校验的整数 id 列表（非整数 / 越界值已被忽略）。
        - ``mode``：时间范围模式字符串（已 strip，可能为 ``""``）。
    """
    json_data = request.get_json(silent=True)
    if json_data:
        ids_field = json_data.get("ids")
        mode = json_data.get("mode")
    else:
        # 普通表单兜底：支持多值 ``ids`` 或逗号分隔单值。
        ids_field = request.form.getlist("ids")
        if not ids_field:
            single = request.form.get("ids")
            ids_field = single.split(",") if single else []
        mode = request.form.get("mode")

    valid_ids: List[int] = []
    if ids_field:
        if isinstance(ids_field, str):
            ids_field = [ids_field]
        for raw in ids_field:
            try:
                valid_ids.append(int(raw))
            except (TypeError, ValueError):
                # 非整数 id 直接忽略（绝不猜测 / 越界删除）。
                pass

    mode = (mode or "").strip()
    return valid_ids, mode


@bp.route("/clean", endpoint="clean", methods=["POST"])
def clean():
    """清理转存记录：批量删除选中 ids，或按时间范围（7d / 30d / all）删除。

    安全护栏：
    - 无有效 ``ids`` 且无合法 ``mode`` 时返回 400 JSON，绝不删除任何行。
    - ``mode='all'`` 仅在已通过前端二次确认（且后端条件校验）后触发。

    响应：
    - XHR 请求：返回 ``jsonify({"deleted": n})``，HTTP 200。
    - 非 XHR（普通表单兜底）：flash 成功提示后 ``redirect`` 回列表页。
    """
    svc = _services()
    valid_ids, mode = _parse_clean_input()
    is_xhr = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"

    with svc.session_factory() as db:
        if valid_ids:
            # 模式 A：批量删除选中（仅删 TransferRecord，不影响其它表）。
            q = db.query(TransferRecord).filter(TransferRecord.id.in_(valid_ids))
        elif mode in ("7d", "30d", "all"):
            # 模式 B：按时间范围。
            q = db.query(TransferRecord)
            if mode != "all":
                days = 7 if mode == "7d" else 30
                cutoff = utc_now() - timedelta(days=days)
                q = q.filter(TransferRecord.created_at < cutoff)
        else:
            # 其它情况：缺少有效的删除条件 -> 安全返回 400，不删。
            return jsonify({"error": "缺少有效的删除条件"}), 400

        deleted = q.delete(synchronize_session=False)
        db.commit()

    if is_xhr:
        return jsonify({"deleted": int(deleted)}), 200

    flash(f"已清理 {int(deleted)} 条转存记录", "success")
    return redirect(url_for("logs.index"))
