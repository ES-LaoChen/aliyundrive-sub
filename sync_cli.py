"""同步作业命令行工具：前台运行并实时打印进度。

用法：
    python sync_cli.py --job-id N
    python sync_cli.py --job-id N --operator "CLI手动"

复用 JobClient 同步引擎（不依赖 Web）；运行期间每 1s 轮询
``SyncService.get_job_progress``，在终端以单行刷新展示整体进度、当前文件、
速度、剩余时间与成功/失败统计。支持 Ctrl-C 中止（向作业发送 break_flag）。

注意：本脚本需要可读的 DATABASE_URL / 存储挂载配置，直接从 .env 或环境变量
装配 Settings（与 app.py 一致）。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time

from config import Settings
from db import get_session_local, init_db, init_engine
from web.services import Services


def _fmt_size(b: float) -> str:
    b = float(b or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return ("%.0f" if b >= 10 or i == 0 else "%.1f") % b + " " + units[i]


def _fmt_eta(sec: float) -> str:
    sec = int(round(sec or 0))
    if sec <= 0:
        return "--"
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def _build_services() -> Services:
    settings = Settings()
    init_engine(settings.DATABASE_URL)
    init_db()
    session_factory = get_session_local()
    from core.sync.service import SyncService

    sync_service = SyncService(session_factory, services=None)
    services = Services(
        settings=settings,
        session_factory=session_factory,
        sync_service=sync_service,
    )
    sync_service.services = services
    return services


_line_len = 0


def _print_line(text: str) -> None:
    """单行刷新（\\r 覆盖），避免刷屏。"""
    global _line_len
    pad = max(0, _line_len - len(text))
    sys.stdout.write("\r" + text + " " * pad)
    sys.stdout.flush()
    _line_len = len(text)


def run(job_id: int, operator: str) -> int:
    services = _build_services()
    svc = services.sync_service

    # 触发运行（异步线程执行，本函数随后轮询）。
    try:
        svc.do_job_manual(job_id, operator=operator)
    except Exception as exc:  # 触发阶段错误（如作业不存在 / 已禁用）
        print("触发失败：%s" % exc)
        return 1

    print("开始同步作业 #%d …（Ctrl-C 可中止）" % job_id)
    started = False
    last_summary_key = None
    try:
        while True:
            data = svc.get_job_progress(job_id)
            if not data.get("running"):
                if started:
                    # 曾经在跑，现在结束。
                    _print_line("同步结束。")
                    print()
                    break
                # 尚未开始（触发后极短窗口），稍等。
                time.sleep(0.3)
                continue
            started = True
            s = data.get("summary", {})
            active = data.get("active", [])
            cur = ""
            if active:
                a = active[0]
                cur = " 当前: %s (%s%%)" % (a.get("fileName", ""), a.get("progress", 0))
            speed = sum(float(x.get("speed") or 0) for x in active)
            remain = max(0, (s.get("totalSize", 0) or 0) - (s.get("transferredSize", 0) or 0))
            eta = remain / speed if speed > 0 else 0
            line = ("进度 %3d%%  完成 %d/%d  成功 %d 失败 %d 传输中 %d  "
                    "速度 %s/s ETA %s%s") % (
                s.get("percent", 0), s.get("done", 0), s.get("total", 0),
                s.get("success", 0), s.get("failed", 0), s.get("running", 0),
                _fmt_size(speed), _fmt_eta(eta), cur,
            )
            _print_line(line)
            # 聚合状态变化才换行打印一次摘要，避免刷屏丢失关键信息。
            key = (s.get("success"), s.get("failed"), s.get("done"))
            if key != last_summary_key:
                last_summary_key = key
                print()
                _print_line(line)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
        print("收到中断信号，正在中止作业 #%d …" % job_id)
        try:
            svc.abort_job(job_id)
        except Exception:
            pass
        # 等待后台线程把中止状态落盘。
        for _ in range(10):
            time.sleep(1.0)
            if not svc.get_job_progress(job_id).get("running"):
                break
        print("已发送中止。")
        return 130

    # 结束输出最终统计。
    data = svc.get_job_progress(job_id)
    s = data.get("summary", {})
    print("最终结果：完成 %d/%d  成功 %d  失败 %d  已恢复(历史) %d  数据量 %s" % (
        s.get("done", 0), s.get("total", 0), s.get("success", 0),
        s.get("failed", 0), data.get("recovered", 0),
        _fmt_size(s.get("transferredSize", 0)),
    ))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同步作业命令行运行器（实时进度）")
    parser.add_argument("--job-id", type=int, required=True, help="要运行的同步作业 ID")
    parser.add_argument("--operator", type=str, default="CLI手动", help="操作人员/触发来源标记")
    args = parser.parse_args()
    # 允许 Ctrl-C 默认行为（KeyboardInterrupt 被捕获处理）。
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return run(args.job_id, args.operator)


if __name__ == "__main__":
    sys.exit(main())
