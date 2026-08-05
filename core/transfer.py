"""核心转存编排：SubscriptionChecker（T-D3 主链路改造）。

主链路（见 DESIGN §5.1 时序图）：
订阅 -> 抢 sub_lock -> start_run -> 目标预检 -> 解析分享 -> 列举顶层文件
-> 按 name 去重（可选）-> 创建 transfer_task -> claim_pending -> with_retry(save_file)
-> 成功：rename + finish_task(success) + 同步 TransferRecord + Aria2
-> 失败：finish_task(failed, error_kind, last_error)
-> 收尾：finish_run + send_transfer_summary

并内置「限时分享临期提醒 / 失效标记」（T10）。

T-D3 改造点：
- 主循环改为 ``_transfer_one`` 单元（单文件粒度）
- 任务状态机：pending → running → {success, failed, skipped}
- ``with_retry`` 装饰 ``save_file`` 调用
- ``_apply_skip_by_name`` 在目标目录按 name+size 命中即 skipped
- ``_precheck_target`` 用 ``TargetCache`` 缓存目录存在性
- 收尾写 ``Run.summary`` + 通知（``send_transfer_summary`` 在 T-D4 完善）
- 保留 ``_add_record`` 兼容逻辑：成功的 task 同步写一条 ``TransferRecord``，
  保证 ``_load_existing_ids`` 既有行为不变

业务层依赖全部通过构造函数注入；新字段（``transfer_repo / target_cache /
sub_lock / retry_policy``）使用 ``getattr`` 风格的「None = 退化到旧行为」兜底，
使旧单测不传这些字段时仍能跑通（旧 ``test_transfer.py`` 由 T-D3 改写 + 扩展）。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from core.aria2 import Aria2Client
from core.log_fields import (
    EVT_RUN_END,
    EVT_RUN_START,
    EVT_TARGET_MISSING,
    EVT_TRANSFER_FAIL,
    EVT_TRANSFER_OK,
    EVT_TRANSFER_SKIP,
    Timer,
    log_event,
)
from core.naming import NamingRule
from core.notifier import NotifierManager
from core.retry import RetryPolicy, with_retry
from core.sub_lock import SubLockManager
from core.target_cache import TargetCache
from core.transfer_repo import (
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SUCCESS,
    TASK_FAILED,
    TASK_PENDING,
    TASK_SKIPPED,
    TASK_SUCCESS,
    TransferRepo,
)
from core.types import (
    ShareExpiredError,
    ShareFile,
    TransferError,
    TransferErrorKind,
)
from db import utc_now
from models import Run, Subscription, TransferRecord, TransferTask

logger = logging.getLogger(__name__)


def _strip_ext(name: str) -> str:
    """去掉扩展名得到 base 名，用于「忽略格式」去重。

    无扩展名或纯点文件名（如 ``.gitignore``）原样返回。
    """
    if not name:
        return name
    base, _ext = os.path.splitext(name)
    return base


class SubscriptionChecker:
    """单个订阅的转存编排器。

    新增可选依赖（保持向前兼容）：
    - ``transfer_repo``: 任务/运行仓储；为 None 时退化为「仅写 TransferRecord」
    - ``target_cache``: 目标目录预检缓存；为 None 时每次 ``list_files`` 直接探测
    - ``sub_lock``: 订阅级并发锁（被 ``SchedulerService`` 用；checker 内部不再抢）
    - ``retry_policy``: 转存重试策略；为 None 时不重试（1 次）
    """

    def __init__(
        self,
        client,  # AliyunClient
        naming: NamingRule,
        notifier: NotifierManager,
        aria2: Aria2Client,
        session_factory: Callable[[], Session],
        share_expire_threshold_days: int = 7,
        transfer_repo: Optional[TransferRepo] = None,
        target_cache: Optional[TargetCache] = None,
        sub_lock: Optional[SubLockManager] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self._client = client
        self._naming = naming
        self._notifier = notifier
        self._aria2 = aria2
        self._session_factory = session_factory
        self._threshold_days = share_expire_threshold_days
        self._repo = transfer_repo
        self._target_cache = target_cache
        self._sub_lock = sub_lock
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=1, retriable_kinds=set())

    # ===================== 主入口 =====================
    def check(self, sub: Subscription, run_mode: str = "scheduled") -> Any:
        """执行一次订阅检查与转存。

        完整流程（见 DESIGN §5.1）：
            1. 抢 sub_lock（无锁管理器时跳过）
            2. start_run
            3. _precheck_target → 失败 finish_run('failed') + error 通知 + return
            4. resolve_share + list_share_files_recursive（递归遍历整棵分享目录树）
            5. 顶层 file 去重：按 source_file_id + name+size
            5.5 载入已转存历史的目标名（去扩展名）集合，用于「忽略格式」去重
            6. for sf in to_process:
                  - skip-by-name → create_task(skipped) + finish_task(skipped)
                  - else: create_task(pending) → claim_pending → with_retry(save_file) →
                    成功：finish_task(success) + rename + TransferRecord
                    失败：finish_task(failed)
            7. finish_run + send_transfer_summary

        Returns:
            ``Run`` ORM 对象（含 status / summary）；无 repo 时返回 None。
        """
        run: Optional[Any] = None
        run_id: Optional[int] = None
        summary = {"added": 0, "skipped": 0, "failed": 0, "pending": 0, "renamed": 0}
        t_total = Timer()
        try:
            if not self._precheck_target(sub):
                # 预检失败：单条 run(failed) + 通知
                run = self._start_run(sub.id, run_mode)
                run_id = run.id
                summary["failed"] = 1
                self._finish_run(run_id, RUN_FAILED, summary)
                if isinstance(run, _RunHandle):
                    run.reload(self._session_factory)
                log_event(
                    EVT_TARGET_MISSING,
                    level=logging.WARNING,
                    subscription_id=sub.id,
                    run_id=run_id,
                )
                self._notifier.send(
                    "目标目录缺失",
                    f"订阅「{sub.name}」的目标目录不存在或无访问权限，已跳过本次。",
                    "error",
                )
                return run

            share_id, share_token = self._client.resolve_share(sub.share_url)
            self._update_share_expire(sub.id, share_id)

            # 关键：从 share_url 抽取 /folder/<id> 段作为起始 parent_file_id。
            # 用户分享链接常带子目录（如 /folder/<hex>），不抽出来会只看到顶层 folder
            # 而漏掉真实文件。
            parent_id = self._client.share_parser.extract_parent_id(sub.share_url)
            # 递归遍历整棵分享目录树（含多层子文件夹），扁平收集所有叶文件。
            # 扁平化：所有文件统一落到 sub.target_folder_id（见 _transfer_one），
            # 忽略原始子目录层级。递归已仅返回 file，下面过滤仅作防御。
            share_files = self._client.list_share_files_recursive(
                share_id, share_token, parent_id
            )
            top_files = [f for f in share_files if f.type == "file"]

            logger.info(
                "订阅「%s」分享解析完成：分享内文件总数=%d，目录树文件(去目录)=%d",
                sub.name, len(share_files), len(top_files),
            )

            existing = self._load_existing_ids(sub.id)
            # 本次 run 的「已处理 source_file_id 集合」：直接复用 existing（与主 _diff 同源），
            # 透传给 _transfer_one 做运行内自包含去重，拦下 to_transfer 中意外的重复 file_id。
            processed_ids = existing
            # 载入已转存历史的目标名（忽略扩展名）集合，供「转存前按历史同名去重」使用。
            dedup_basenames = self._load_existing_target_basenames(sub.id)
            to_transfer = self._diff(top_files, existing)
            if not to_transfer:
                # 空源：不开新 run（不开通知）—— 与 PRD §T03 一致
                logger.info("订阅「%s」无新增文件", sub.name)
                return None

            run = self._start_run(sub.id, run_mode)
            run_id = run.id

            log_event(
                EVT_RUN_START,
                level=logging.INFO,
                subscription_id=sub.id,
                run_id=run_id,
                extra={"file_count": len(to_transfer)},
            )

            download_urls: list[str] = []
            for sf in to_transfer:
                # 解析目标盘 ID（默认走 sub.target_drive_type 对应盘，无则用默认盘）
                target_drive_id = self._resolve_target_drive_id(sub)
                outcome, target_file_id, renamed = self._transfer_one(
                    sub, sf, share_id, share_token, run_id, target_drive_id=target_drive_id,
                    dedup_basenames=dedup_basenames, processed_ids=processed_ids,
                )
                if outcome == "success":
                    summary["added"] += 1
                    if renamed:
                        summary["renamed"] += 1
                    if target_file_id and self._aria2.enabled:
                        try:
                            download_urls.append(self._client.get_download_url(target_file_id))
                        except Exception:
                            logger.debug("获取下载直链失败（忽略）: %s", target_file_id)
                elif outcome == "skipped":
                    summary["skipped"] += 1
                elif outcome == "failed":
                    summary["failed"] += 1

            if self._aria2.enabled and download_urls:
                self._aria2.add_uri(download_urls, {"dir": sub.target_folder_path or ""})

            # 收尾 run
            run_status = (
                RUN_SUCCESS
                if summary["failed"] == 0
                else RUN_PARTIAL
                if summary["added"] > 0 or summary["skipped"] > 0
                else RUN_FAILED
            )
            self._finish_run(run_id, run_status, summary)
            # 刷新 handle（仅当是 _RunHandle 时；stub 时 id=None 跳过）
            if isinstance(run, _RunHandle):
                run.reload(self._session_factory)
            log_event(
                EVT_RUN_END,
                level=logging.INFO,
                subscription_id=sub.id,
                run_id=run_id,
                elapsed_ms=t_total.elapsed_ms(),
                extra=summary,
            )

            # 通知（与既有行为兼容：保留「转存完成」标题，level 按失败数切换）
            self._send_run_summary_notification(sub, summary, run_status)
            self._check_expire_warning(sub.id)
            # SubStatus：成功转存有新增文件 → 刷新「最后成功转存时间」，
            # 供状态巡检的完结超时判定使用（共享 existing 同源集合，无额外 IO）。
            if run_status in (RUN_SUCCESS, RUN_PARTIAL) and summary["added"] > 0:
                self._update_last_transfer(sub.id)
        except ShareExpiredError as exc:
            self._mark_pending_update(sub.id)
            self._notifier.send(
                "分享待更新",
                f"订阅「{sub.name}」的分享链接已失效，请替换。\n({exc.message})",
                "warn",
            )
        except TransferError as exc:
            # with_retry 抛出的 ShareExpired / 不重试业务错误 → 标记 pending_update
            if exc.kind == TransferErrorKind.SHARE_EXPIRED:
                self._mark_pending_update(sub.id)
                self._notifier.send(
                    "分享待更新",
                    f"订阅「{sub.name}」的分享链接已失效，请替换。\n({exc.message})",
                    "warn",
                )
            else:
                logger.warning("转存 TransferError: kind=%s msg=%s", exc.kind.value, exc.message)
        except Exception as exc:
            logger.exception("订阅检查异常: %s", getattr(sub, 'name', sub.id))
            # 异常分支也写一条 run(failed) 记录，让运行历史能看到
            try:
                if run_id is None:
                    fail_run = self._start_run(sub.id, run_mode)
                    run_id = fail_run.id
                    run = fail_run  # 同步更新外部 run 变量
                summary["failed"] = 1
                self._finish_run(run_id, RUN_FAILED, summary)
                if isinstance(run, _RunHandle):
                    run.reload(self._session_factory)
            except Exception as inner:
                logger.warning("异常分支写 run 失败: %s", inner)
            self._notifier.send(
                "检查失败",
                f"订阅「{getattr(sub, 'name', sub.id)}」检查异常：{exc}",
                "error",
            )
        finally:
            self._update_last_check(sub.id)
        return run

    # ===================== 单文件单元 =====================
    def _transfer_one(
        self,
        sub: Subscription,
        sf: ShareFile,
        share_id: str,
        share_token: str,
        run_id: Optional[int] = None,
        target_drive_id: Optional[str] = None,
        dedup_basenames: Optional[set[str]] = None,
        processed_ids: Optional[set[str]] = None,
    ) -> tuple[str, Optional[str], bool]:
        """处理单文件：去重 → claim → retry save → finish。

        Args:
            dedup_basenames: 已成功/跳过的转存历史目标名（去扩展名）集合，用于「忽略格式」
                去重。由 ``check()`` 在单次运行中复用同一对象，使同一次运行内同名（不同扩展名）
                的文件也能互相拦下。为 None 时退化为每次重新从 DB 载入（保持旧调用方式可用）。
            processed_ids: 本次 run 已处理过的 source_file_id 集合，用于运行内自包含去重。
                由 ``check()`` 复用主 ``existing`` 同源集合并透传；集合内任意分支（成功/各类
                跳过）命中后均会 ``add(sf.file_id)``，使运行内的重复 file_id 被直接拦下，
                不再依赖每次查 DB。为 None 时退化为每次 ``self._load_existing_ids(sub.id)``
                原逻辑（保持旧调用方式可用）。

        Returns:
            ``(outcome, target_file_id, renamed)`` 其中 ``outcome ∈ {success, skipped, failed}``。
            失败/跳过时 ``target_file_id`` 为 None；成功时为新 file_id；``renamed`` 表示是否改名。
        """
        # 0) 最顶部计算目标文件名（改名在 copy 时一次性完成，任何 skip 判断之前）。
        new_name = self._naming.compute_new_name(
            sf.name, sub.rename_mode,
            template=sub.naming_template, regex=sub.naming_regex,
            prefix=sub.rename_prefix, suffix=sub.rename_suffix,
        )
        renamed = bool(new_name) and new_name != sf.name

        # 已转存历史目标名（忽略扩展名）集合：复用外部传入的同一对象，以支持同一次
        # check() 运行内「后面出现的同名(不同扩展名)文件也被拦下」的集合自维护。
        basenames = dedup_basenames if dedup_basenames is not None else self._load_existing_target_basenames(sub.id)

        # 0.5) 按转存历史「目标文件名（忽略扩展名）」去重：同名(无视格式)已转存过则跳过
        if _strip_ext(new_name) in basenames:
            # 将本次 basename 加入集合，确保同一次运行内后续同名文件也被拦下
            basenames.add(_strip_ext(new_name))
            # 同步记入运行内已处理集合（与 basenames 对称，自包含去重）
            if processed_ids is not None:
                processed_ids.add(sf.file_id)
            self._record_skipped_duplicate_name(sub, sf, new_name, run_id)
            log_event(
                EVT_TRANSFER_SKIP, level=logging.INFO,
                subscription_id=sub.id, run_id=run_id,
                source_file_id=sf.file_id, source_name=sf.name,
                extra={"reason": "skipped_duplicate_name", "target_name": new_name},
            )
            return "skipped", None, False

        # 1) 按 name 去重（命中「新文件名」同名同 size → 跳过；不调 save_file）
        #    透传 target_drive_id，确保 live name+size 去重命中的是与 save_file 相同的目标盘
        if self._apply_skip_by_name(sub.target_folder_id, new_name, sf, target_drive_id):
            # 将本次 basename 加入集合（同一次运行内自维护）
            basenames.add(_strip_ext(new_name))
            # 同步记入运行内已处理集合（与 basenames 对称，自包含去重）
            if processed_ids is not None:
                processed_ids.add(sf.file_id)
            self._record_skipped_by_name(sub, sf, run_id)
            log_event(
                EVT_TRANSFER_SKIP,
                level=logging.INFO,
                subscription_id=sub.id,
                run_id=run_id,
                source_file_id=sf.file_id,
                source_name=sf.name,
                extra={"reason": "skipped_existing"},
            )
            return "skipped", None, False

        # 2) 旧去重：已成功/跳过的不再转存。
        #    优先用运行内 processed_ids（自包含、无 DB 往返，直接拦下 to_transfer 中的重复 file_id）；
        #    否则退化为每次查 DB（保持旧调用方式可用）。
        if processed_ids is not None and sf.file_id in processed_ids:
            # 将本次 basename 加入集合（同一次运行内自维护）
            basenames.add(_strip_ext(new_name))
            self._record_skipped_existing(sub, sf, run_id)
            return "skipped", None, False
        with self._session_factory() as db:
            existing = self._load_existing_ids(sub.id)
        if sf.file_id in existing:
            # 将本次 basename 加入集合（同一次运行内自维护）
            basenames.add(_strip_ext(new_name))
            # 同步记入运行内已处理集合（与 basenames 对称，自包含去重）
            if processed_ids is not None:
                processed_ids.add(sf.file_id)
            self._record_skipped_existing(sub, sf, run_id)
            return "skipped", None, False

        # 3) 创建 task + 领取（new_name 已在方法顶部算好）
        logger.info(
            "开始转存单文件：订阅=%s 源=%s 目标名=%s 盘=%s",
            sub.name, sf.name, new_name, target_drive_id or "default",
        )
        task = self._create_task_pending(sub, sf, new_name, run_id)
        task_id = task.id
        claimed = self._claim_task(task_id)
        if not claimed:
            # 其他协程已抢走 or 已用尽重试
            logger.debug("任务 %s 未领取（可能并发抢走或已用尽重试）", task_id)
            return "skipped", None, False
        # 5) with_retry 包 save_file
        t = Timer()
        try:
            target_file_id = with_retry(
                self._retry_policy,
                self._client.save_file,
                sub.target_folder_id,
                sf,
                share_id,
                share_token,
                drive_id=target_drive_id,  # 目标盘 ID（keyword 避免位置参数冲突）
                new_name=new_name,  # 改名随 copy 一次性完成
            )
        except TransferError as exc:
            # 第一次碰到 ShareExpiredError 时就把订阅置 pending_update（与 resolve_share 一致）
            if exc.kind == TransferErrorKind.SHARE_EXPIRED:
                self._mark_pending_update(sub.id)
                self._notifier.send(
                    "分享待更新",
                    f"订阅「{sub.name}」的分享链接已失效，请替换。\n({exc.message})",
                    "warn",
                )
            self._finish_task_failed(task_id, exc)
            # 同步写 TransferRecord(failed)，保留老表去重语义
            self._add_record(
                sub.id, sf, "", sf.name, "failed",
                f"{exc.kind.value}: {exc.message}"[:500], False,
            )
            log_event(
                EVT_TRANSFER_FAIL,
                level=logging.ERROR,
                subscription_id=sub.id,
                run_id=run_id,
                source_file_id=sf.file_id,
                source_name=sf.name,
                attempts=exc.attempts,
                error_kind=exc.kind.value,
                elapsed_ms=t.elapsed_ms(),
                message=exc.message[:200],
            )
            return "failed", None, False
        except Exception as exc:  # 兜底：未知异常
            kind = TransferErrorKind.UNKNOWN
            te = TransferError(kind=kind, message=str(exc), attempts=1, original=exc)
            self._finish_task_failed(task_id, te)
            self._add_record(
                sub.id, sf, "", sf.name, "failed",
                f"unknown: {exc}"[:500], False,
            )
            log_event(
                EVT_TRANSFER_FAIL,
                level=logging.ERROR,
                subscription_id=sub.id,
                run_id=run_id,
                source_file_id=sf.file_id,
                source_name=sf.name,
                attempts=1,
                error_kind=kind.value,
                elapsed_ms=t.elapsed_ms(),
                message=str(exc)[:200],
            )
            return "failed", None, False

        # 6) 成功：改名已在 copy 时完成（new_name 已传给 save_file），无需再调 rename_file。
        # 将本次 basename 加入集合（同一次运行内自维护：后续同名(不同扩展名)文件被拦下）
        basenames.add(_strip_ext(new_name))
        # 同步记入运行内已处理集合（与 basenames 对称，自包含去重）
        if processed_ids is not None:
            processed_ids.add(sf.file_id)
        self._finish_task_success(task_id, target_file_id, new_name)
        self._add_record(
            sub.id, sf, target_file_id, new_name or sf.name, "success", "", renamed
        )
        log_event(
            EVT_TRANSFER_OK,
            level=logging.INFO,
            subscription_id=sub.id,
            run_id=run_id,
            source_file_id=sf.file_id,
            source_name=sf.name,
            target_file_id=target_file_id,
            elapsed_ms=t.elapsed_ms(),
            extra={"renamed": renamed},
        )
        return "success", target_file_id, renamed

    # ===================== 内部步骤 =====================
    def _diff(
        self, share_files: list[ShareFile], existing_ids: set[str]
    ) -> list[ShareFile]:
        """过滤掉已成功转存过的文件（按 source_file_id 去重）。"""
        return [f for f in share_files if f.file_id not in existing_ids]

    def _apply_skip_by_name(
        self,
        target_folder_id: str,
        desired_name: str,
        sf: ShareFile,
        drive_id: Optional[str] = None,
    ) -> bool:
        """T10：按 desired_name + size 命中目标目录已有文件 → 跳过。

        仅当 ``target_folder_id`` 非空时启用（root 视为有效）。

        Args:
            drive_id: 目标盘 ID；透传给 ``list_files`` 以与 ``save_file`` 命中同一目标盘。
                为 None 时 ``list_files`` 走默认盘（兼容老逻辑与既有测试）。
        """
        if not target_folder_id:
            return False
        try:
            files = self._client.list_files(parent_file_id=target_folder_id, drive_id=drive_id)
        except Exception as exc:
            # 列失败按未命中处理（不阻断主流程）
            logger.debug(
                "list_files(target=%s, drive=%s) 失败: %s",
                target_folder_id, drive_id, exc,
            )
            return False
        for f in files:
            if f.name == desired_name and f.size == sf.size and f.type == "file":
                return True
        return False

    def _precheck_target(self, sub: Subscription) -> bool:
        """T08：目标目录预检；命中缓存或探测后返回是否可达。

        支持多盘探测：若 sub.target_drive_type 为空（老数据/未指定盘），
        自动在 default / resource / backup 三个盘上探测，找到后回写 DB。
        """
        if not sub.target_folder_id:
            return True  # 无目标视为通过（保留旧行为）

        def probe(folder_id: str, drive_id: Optional[str] = None) -> bool:
            try:
                self._client.list_files(parent_file_id=folder_id, drive_id=drive_id)
                return True
            except Exception:
                return False

        # 缓存键：仅用 folder_id（与 TargetCache.exists_or_probe 约定一致）。
        # cache 含义："该 folder_id 至少在某个盘上存在"——命中即返 True。
        cache_key = sub.target_folder_id
        if self._target_cache is not None:
            # 注意：cache 命中只代表"曾经探测通过"（在某个盘上存在），
            # 即便用户后来改了订阅的 target_drive_type，cache 仍正确——
            # 因为 folder_id 存在性不依赖 drive 归属。
            cached = self._target_cache.peek(cache_key)
            if cached is not None:
                exists, _ts = cached
                return exists

        # 1. 如果有指定盘，直接探测该盘
        if sub.target_drive_type:
            try:
                drives = self._client.list_drives()
            except Exception:
                drives = []
            result = False
            if drives:
                for d in drives:
                    if d["drive_type"] == sub.target_drive_type:
                        result = probe(sub.target_folder_id, d["drive_id"])
                        break
            if not result and not drives:
                # list_drives 不可用时退到旧行为
                result = probe(sub.target_folder_id)
            if self._target_cache is not None:
                self._target_cache.exists_or_probe(cache_key, lambda fid: result)
            return result

        # 2. 老数据 fallback：在所有盘上探测
        try:
            drives = self._client.list_drives()
        except Exception:
            drives = []
        if not drives:
            # list_drives 不可用（测试 stub / token 失效）→ 退回旧行为
            result = probe(sub.target_folder_id)
            if self._target_cache is not None:
                self._target_cache.exists_or_probe(cache_key, lambda fid: result)
            return result
        # 优先探测 default（最快路径），再 resource / backup
        order = ["default", "resource", "backup"]
        drives_by_type = {d["drive_type"]: d["drive_id"] for d in drives}
        for dtype in order:
            did = drives_by_type.get(dtype)
            if not did:
                continue
            if probe(sub.target_folder_id, did):
                # 命中后回写 DB（懒迁移）。先 snapshot 字段避免 detached
                try:
                    sub_id_snapshot = sub.id
                except Exception:
                    sub_id_snapshot = None
                self._backfill_target_drive_type(sub_id_snapshot, dtype)
                if self._target_cache is not None:
                    self._target_cache.exists_or_probe(cache_key, lambda fid: True)
                return True
        # 全部失败
        if self._target_cache is not None:
            self._target_cache.exists_or_probe(cache_key, lambda fid: False)
        return False

    def _backfill_target_drive_type(self, sub_id: int, drive_type: str) -> None:
        """懒迁移：探测到目标目录所属盘后写回 subscriptions.target_drive_type。"""
        try:
            with self._session_factory() as db:
                sub = db.get(Subscription, sub_id)
                if sub is not None and not sub.target_drive_type:
                    sub.target_drive_type = drive_type
                    db.commit()
                    logger.info(
                        "已懒迁移订阅 #%d 目标盘类型 → %s", sub_id, drive_type
                    )
        except Exception as exc:
            logger.warning("懒迁移目标盘类型失败: sub=%s err=%s", sub_id, exc)

    def _load_existing_ids(self, sub_id: int) -> set[str]:
        """已成功 / 跳过转存的 source_file_id 集合（兼容旧 _add_record 逻辑）。

        同时读取两张独立记录表并取并集：
          (1) 转存记录表 transfer_records（TransferRecord）
          (2) 订阅的转存任务表 transfer_tasks（TransferTask）
        两处任一存在「成功/跳过」记录即视为已转存，参与去重，避免只查一处漏查。
        """
        ids: set[str] = set()
        with self._session_factory() as db:
            rec_rows = (
                db.query(TransferRecord.source_file_id)
                .filter(
                    TransferRecord.subscription_id == sub_id,
                    TransferRecord.status.in_(["success", "skipped"]),
                )
                .all()
            )
            task_rows = (
                db.query(TransferTask.source_file_id)
                .filter(
                    TransferTask.subscription_id == sub_id,
                    TransferTask.status.in_(["success", "skipped"]),
                )
                .all()
            )
        ids.update(r[0] for r in rec_rows if r[0])
        ids.update(r[0] for r in task_rows if r[0])
        return ids

    def _load_existing_target_basenames(self, sub_id: int) -> set[str]:
        """已成功/跳过转存的目标名（去扩展名）集合，用于「忽略格式」去重。

        读取两处数据源并取并集：
          (1) 转存记录表 transfer_records（TransferRecord）
          (2) 订阅的转存任务表 transfer_tasks（TransferTask）
        两处任一存在同名（忽略扩展名）的成功/跳过记录，即视为已转存并参与去重，
        避免「只查一处导致另一处漏查而重复转存」。
        failed 状态表示并未真正转存成功，必须允许重试，不参与去重。
        """
        result: set[str] = set()
        with self._session_factory() as db:
            rec_rows = (
                db.query(TransferRecord.target_name)
                .filter(
                    TransferRecord.subscription_id == sub_id,
                    TransferRecord.status.in_(["success", "skipped"]),
                )
                .all()
            )
            task_rows = (
                db.query(TransferTask.target_name)
                .filter(
                    TransferTask.subscription_id == sub_id,
                    TransferTask.status.in_(["success", "skipped"]),
                )
                .all()
            )
        result.update(_strip_ext(r[0]) for r in rec_rows if r[0])
        result.update(_strip_ext(r[0]) for r in task_rows if r[0])
        return result

    def _resolve_target_drive_id(self, sub: Subscription) -> Optional[str]:
        """根据 sub.target_drive_type 解析出真实的 drive_id。

        返回 None 时 save_file 走默认盘。
        """
        if not sub.target_drive_type:
            return None
        try:
            drives = self._client.list_drives()
        except Exception:
            return None
        for d in drives:
            if d["drive_type"] == sub.target_drive_type:
                return d["drive_id"]
        return None


    # ===================== Task 包装（_transfer_one 用） =====================
    def _create_task_pending(
        self, sub: Subscription, sf: ShareFile, new_name: str, run_id: Optional[int]
    ) -> Any:
        """新建 pending task。返回 ``_TaskHandle``（id 在 session 内取）。"""
        with self._session_factory() as db:
            if self._repo is None:
                # 无 repo：仅返一个轻量占位对象，外部不再依赖 task.id
                return _TaskStub()
            task = self._repo.create_task(
                db, sub.id, sf.file_id, sf.name, new_name or sf.name,
                run_id=run_id, status=TASK_PENDING,
            )
            db.commit()
            tid = task.id
        return _TaskHandle(id=tid)

    def _claim_task(self, task_id: int) -> Optional[Any]:
        """原子领取。"""
        if not isinstance(task_id, int):
            return _TaskStub(claimed=True)
        with self._session_factory() as db:
            if self._repo is None:
                return _TaskStub(claimed=True)
            claimed = self._repo.claim_pending(
                db, task_id,
                max_attempts=self._retry_policy.max_attempts, commit=True,
            )
            return claimed is not None

    def _finish_task_success(
        self, task_id: int, target_file_id: str, target_name: str
    ) -> None:
        if not isinstance(task_id, int):
            return
        with self._session_factory() as db:
            if self._repo is None:
                return
            self._repo.finish_task(
                db, task_id, TASK_SUCCESS,
                target_file_id=target_file_id, target_name=target_name,
                commit=True,
            )

    def _finish_task_failed(self, task_id: int, exc: TransferError) -> None:
        if not isinstance(task_id, int):
            return
        with self._session_factory() as db:
            if self._repo is None:
                return
            self._repo.finish_task(
                db, task_id, TASK_FAILED,
                last_error=f"{exc.kind.value}: {exc.message}"[:500],
                error_kind=exc.kind.value,
                commit=True,
            )

    def _record_skipped_by_name(
        self, sub: Subscription, sf: ShareFile, run_id: Optional[int]
    ) -> None:
        if self._repo is None:
            # 退化：写一条 TransferRecord(skipped)
            self._add_record(
                sub.id, sf, "", sf.name, "skipped", "skipped_existing", False
            )
            return
        with self._session_factory() as db:
            self._repo.create_task(
                db, sub.id, sf.file_id, sf.name, sf.name,
                run_id=run_id, status=TASK_SKIPPED, commit=True,
            )
            self._add_record(
                sub.id, sf, "", sf.name, "skipped", "skipped_existing", False
            )

    def _record_skipped_existing(
        self, sub: Subscription, sf: ShareFile, run_id: Optional[int]
    ) -> None:
        if self._repo is None:
            return
        with self._session_factory() as db:
            self._repo.create_task(
                db, sub.id, sf.file_id, sf.name, sf.name,
                run_id=run_id, status=TASK_SKIPPED, commit=True,
            )

    def _record_skipped_duplicate_name(
        self, sub: Subscription, sf: ShareFile, new_name: str, run_id: Optional[int]
    ) -> None:
        """命中「历史同名(忽略扩展名)」去重时的跳过记账。"""
        if self._repo is None:
            # 退化：仅写一条 TransferRecord(skipped)
            self._add_record(
                sub.id, sf, "", new_name or sf.name, "skipped", "skipped_duplicate_name", False
            )
            return
        with self._session_factory() as db:
            self._repo.create_task(
                db, sub.id, sf.file_id, sf.name, new_name or sf.name,
                run_id=run_id, status=TASK_SKIPPED, commit=True,
            )
            self._add_record(
                sub.id, sf, "", new_name or sf.name, "skipped", "skipped_duplicate_name", False
            )

    # ===================== Run 包装 =====================
    def _start_run(self, sub_id: int, run_mode: str) -> Any:
        with self._session_factory() as db:
            if self._repo is None:
                return _RunStub(sub_id=sub_id, run_mode=run_mode)
            run = self._repo.start_run(db, sub_id, run_mode=run_mode)
            db.commit()
            # 立刻在 session 内拷贝关键字段，避免 detached
            run_id = run.id
        return _RunHandle(id=run_id, sub_id=sub_id, run_mode=run_mode)

    def _finish_run(self, run_id: int, status: str, summary: dict) -> None:
        if not isinstance(run_id, int):
            return
        with self._session_factory() as db:
            if self._repo is None:
                return
            self._repo.finish_run(db, run_id, status, summary, commit=True)

    # ===================== 既有步骤 =====================
    def _add_record(
        self,
        sub_id: int,
        sf: ShareFile,
        target_file_id: str,
        target_name: str,
        status: str,
        message: str,
        renamed: bool,
    ) -> TransferRecord:
        with self._session_factory() as db:
            rec = TransferRecord(
                subscription_id=sub_id,
                source_file_id=sf.file_id,
                source_name=sf.name,
                target_file_id=target_file_id,
                target_name=target_name,
                status=status,
                message=message,
                renamed=renamed,
            )
            db.add(rec)
            db.commit()
            db.refresh(rec)
            return rec

    def _update_last_check(self, sub_id: int) -> None:
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
            if sub:
                sub.last_check_at = utc_now()
                db.commit()

    def _update_last_transfer(self, sub_id: int) -> None:
        """成功转存后刷新「最后成功转存时间」（供 SubStatus 完结超时判定）。

        Args:
            sub_id: 订阅 id。
        """
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
            if sub:
                sub.last_transfer_at = utc_now()
                db.commit()

    def _mark_pending_update(self, sub_id: int) -> None:
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
            if sub:
                sub.status = "pending_update"
                db.commit()

    def _update_share_expire(self, sub_id: int, share_id: str) -> None:
        try:
            info = self._client.get_share_info(share_id)
        except Exception:
            logger.debug("获取分享元信息失败（忽略）: %s", share_id)
            return
        dt = self._parse_expire(
            info.get("expiration") or info.get("expire_time") or info.get("expired")
        )
        if dt is None:
            return
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
            if sub:
                sub.share_expire_at = dt
                db.commit()

    def _check_expire_warning(self, sub_id: int) -> None:
        """限时分享临期提醒（T10）：剩余 <= 阈值天数则通知。"""
        with self._session_factory() as db:
            sub = db.get(Subscription, sub_id)
            if not sub or not sub.share_expire_at:
                return
            remaining = sub.share_expire_at - utc_now()
            if timedelta(0) <= remaining <= timedelta(days=self._threshold_days):
                days = max(remaining.days, 0)
                self._notifier.send(
                    "分享即将过期",
                    f"订阅「{sub.name}」的分享链接剩余约 {days} 天，请提前准备替换。",
                    "warn",
                )

    def _send_run_summary_notification(
        self, sub: Subscription, summary: dict, run_status: str
    ) -> None:
        """发送转存摘要通知。

        T-D4 使用 ``send_transfer_summary`` 渲染 PRD §4.2 markdown 模板；
        T-D3 之前的旧版「转存完成」标题作为 fallback（无 NotifierManager.send_transfer_summary 时）。
        """
        # 收集失败项
        failed_items: list[dict] = []
        if self._repo is not None and summary.get("failed", 0) > 0:
            try:
                with self._session_factory() as db:
                    # 找本次 run 下 status='failed' 的 task（限制 5 个给通知）
                    from models import TransferTask as TT
                    rows = (
                        db.query(TT)
                        .filter(
                            TT.subscription_id == sub.id,
                            TT.status == "failed",
                        )
                        .order_by(TT.updated_at.desc())
                        .limit(5)
                        .all()
                    )
                    for t in rows:
                        failed_items.append({
                            "name": t.source_name or t.source_file_id,
                            "kind": t.error_kind or "unknown",
                            "attempts": t.attempts or 0,
                            "max_attempts": self._retry_policy.max_attempts,
                            "last_error": (t.last_error or "")[:200],
                        })
            except Exception:
                logger.debug("拉取失败详情失败（忽略）", exc_info=True)

        # 优先用新模板；老实现也保留 fallback
        if hasattr(self._notifier, "send_transfer_summary"):
            self._notifier.send_transfer_summary(
                sub, summary, error_kind_map=None,
                failed_items=failed_items, elapsed_seconds=None,
            )
            return

        # Fallback：旧文本
        level = "info" if summary.get("failed", 0) == 0 else "warn"
        msg = (
            f"订阅「{sub.name}」本次转存 {summary.get('added', 0)} 个成功、"
            f"{summary.get('skipped', 0)} 个跳过、{summary.get('failed', 0)} 个失败。"
        )
        self._notifier.send("转存完成", msg, level)

    @staticmethod
    def _parse_expire(value) -> Optional[datetime]:
        """解析分享过期时间：支持毫秒 / 秒时间戳、ISO 字符串。"""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            v = int(value)
            if v > 1_000_000_000_000:  # 毫秒
                v = v / 1000
            return datetime.fromtimestamp(v, tz=timezone.utc).replace(tzinfo=None)
        if isinstance(value, str):
            s = value.strip()
            if s.isdigit():
                return SubscriptionChecker._parse_expire(int(s))
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19], fmt).replace(tzinfo=None)
                except ValueError:
                    continue
        return None


# ===================== 兼容旧调用方（无 repo）======================
class _TaskStub:
    """当 ``transfer_repo`` 未注入时，``_transfer_one`` 的轻量占位。

    行为：``id`` 字段为非整数（``None``），下游 ``finish_*`` / ``claim_*`` 全部 no-op，
    仅 ``_add_record`` 走老 TransferRecord 路径，保证既有 ``test_transfer.py`` 行为不变。
    """

    def __init__(self, id: Optional[int] = None, claimed: bool = True) -> None:
        self.id = id
        self._claimed = claimed


class _TaskHandle:
    """轻量 task handle：仅在 session 内取一次 id，避免 detached。"""

    def __init__(self, id: int) -> None:
        self.id = id


class _RunStub:
    """当 ``transfer_repo`` 未注入时，主流程的 Run 占位（仅用于日志与返回）。"""

    def __init__(self, sub_id: int, run_mode: str) -> None:
        self.id: Optional[int] = None
        self.subscription_id = sub_id
        self.run_mode = run_mode
        self.status = "unknown"
        self.summary = "{}"


class _RunHandle:
    """轻量 run handle：仅在 session 内取一次 id/字段，避免 detached。

    业务层需要 ``status`` 时调 ``reload(self._session_factory)``。
    """

    def __init__(self, id: int, sub_id: int, run_mode: str) -> None:
        self.id = id
        self.subscription_id = sub_id
        self.run_mode = run_mode
        self.status: str = "running"
        self.summary: str = "{}"

    def reload(self, session_factory: Callable[[], Session]) -> None:
        with session_factory() as db:
            run = db.get(Run, self.id)
            if run is not None:
                self.status = run.status
                self.summary = run.summary
