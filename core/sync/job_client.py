"""同步任务引擎核心：JobClient / JobTask / CopyItem。

移植自 TaoSync ``service/syncJob/jobClient.py``，仅保留「移动模式」(method=2) 分支，
调度支持 interval / cron / 手动。持久化改为 SQLAlchemy（``core.sync.job_dao``）。

``commonUtils`` 的 ``convertSeconds`` / ``convertBytes`` 内联为轻量函数；
``common.LNG.G`` 中文文案直接以常量替换（本项目不引入 i18n）。
"""
from __future__ import annotations

import itertools
import json
import logging
import posixpath
import threading
import time
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

from core.sync import engine as storage_engine
from core.sync.job_dao import (
    add_job_task,
    append_moved_file,
    clear_source_snapshot,
    get_job_by_id,
    get_source_snapshot,
    load_moved_file_names,
    replace_source_snapshot,
    source_snapshot_identity,
    update_job_task_status,
)

logger = logging.getLogger(__name__)


# 轻量单位换算（替代 commonUtils）。
def convert_seconds(seconds):
    seconds = int(seconds or 0)
    hours = seconds // 3600
    remaining = seconds % 3600
    minutes = remaining // 60
    secs = remaining % 60
    return hours, minutes, secs


def convert_bytes(val):
    val = int(val or 0)
    unit_list = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while i < len(unit_list) - 1 and val >= 1024 ** (i + 1):
        i += 1
    return f"{val / (1024 ** i):.2f} {unit_list[i]}"


# 中文文案常量（替代 common.LNG.G）。
class G:
    source_target_overlap = "源路径与目标路径存在重叠，无法同步"
    source_mode_invalid = "sourceMode 取值无效"
    file_size_invalid = "文件大小必须为非负整数"
    file_size_range_invalid = "最小文件大小不能大于最大文件大小"
    move_delivery_incomplete = "目标未全部送达，跳过源删除"
    source_changed_during_move = "源文件在移动期间发生变化，跳过删除"
    source_version_unavailable = "源文件版本指纹缺失，跳过删除"
    copy_success_but_delete_fail = "复制成功但删除源文件失败：{}"
    move_skipped_logged = "文件已记录为已移动，跳过：{fileName}（源：{srcPath}）"
    move_already_logged = "已记录为已移动，跳过"
    scan_error = "扫描{}目录出错：{}"
    src = "源"
    dst = "目标"
    del_job_course_error = "添加任务过程中报错：{}"
    interval_lost = "interval 调度间隔缺失"
    cron_lost = "cron 调度字段缺失"
    do_job_err = "执行作业出错：{}"
    cannot_resume_lost_job = "无法恢复未调度的作业"
    stop_fail = "停止作业失败：{}"
    disable_fail = "禁用作业失败：{}"
    job_running = "作业正在运行中"
    disabled_job_cannot_run = "已禁用的作业不能运行"
    cannot_disable_manual_job = "手动作业不能禁用"
    no_job_for_run = "没有可手动运行的作业"
    disable_then_edit = "请先禁用作业再编辑"
    job_changed_during_sync = "同步期间作业配置发生变化"
    job_not_found = "作业不存在"


def is_file_size_allowed(file_size, min_file_size=None, max_file_size=None):
    if min_file_size is not None and file_size < min_file_size:
        return False
    if max_file_size is not None and file_size > max_file_size:
        return False
    return True


def normalize_virtual_path(path):
    value = str(path).replace("\\", "/")
    return posixpath.normpath("/" + value.lstrip("/")).casefold()


def virtual_paths_overlap(first_path, second_path):
    first = normalize_virtual_path(first_path)
    second = normalize_virtual_path(second_path)
    return (first == second
            or first.startswith(second.rstrip("/") + "/")
            or second.startswith(first.rstrip("/") + "/"))


class CopyItem:
    def __init__(self, src_path, dst_path, file_name, file_size, method, job_task):
        self.job_task = job_task
        self.alist_client = self.job_task.alist_client
        self.task_id = self.job_task.task_id
        self.src_path = src_path
        self.dst_path = dst_path
        self.file_name = file_name
        self.file_size = file_size
        self.copy_type = 0 if method < 2 else 2
        self.alist_task_id = None
        self.status = 0
        self.progress = 0.0
        self.err_msg = None
        self.create_time = int(time.time())
        self.doing_key = None

    def do_by_thread(self):
        do_thread = threading.Thread(target=self.do_it, name="copy-item-" + str(self.task_id), daemon=True)
        do_thread.start()

    def do_it(self):
        try:
            if self.job_task.break_flag:
                self.status = 4
            else:
                self.alist_task_id = self.alist_client.copy_file(
                    self.src_path, self.dst_path, self.file_name
                )
        except Exception as e:
            self.err_msg = str(e)
            self.status = 7
        else:
            if self.alist_task_id is None:
                self.status = 2
            elif self.status != 4:
                self.check_and_get_status()
        self.end_it()

    def check_and_get_status(self):
        while True:
            if self.job_task.break_flag:
                self.status = 4
                if self.alist_task_id is not None:
                    try:
                        self.alist_client.copy_task_cancel(self.alist_task_id)
                        self.alist_client.copy_task_delete(self.alist_task_id)
                    except Exception:
                        self.status = 7
                        self.err_msg = "取消复制任务失败"
                break
            cu_time = time.time()
            time.sleep(0.61 if cu_time - self.job_task.last_watching < 3 else 2.93)
            try:
                task_info = self.alist_client.task_info(self.alist_task_id)
            except Exception as e:
                logger.exception(e)
                e_msg = str(e)
                if "404" in e_msg:
                    e_msg = "任务可能已被删除"
                task_info = {"state": 7, "progress": None, "error": e_msg}
            if task_info["state"] == self.status and task_info["progress"] == self.progress:
                continue
            self.status = task_info["state"]
            self.progress = task_info["progress"]
            self.err_msg = task_info["error"] if task_info["error"] else None
            if task_info["state"] in (2, 4, 7):
                try:
                    self.alist_client.copy_task_delete(self.alist_task_id)
                except Exception:
                    pass
                break

    def end_it(self):
        self.job_task.copy_hook(
            self.src_path, self.dst_path, self.file_name, self.file_size,
            self.alist_task_id, self.status, err_msg=self.err_msg,
            copy_type=self.copy_type, create_time=self.create_time,
        )
        del self.job_task.doing[self.doing_key]


class JobTask:
    def __init__(self, task_id, vm):
        self.task_id = task_id
        self.job_client = vm
        self.job = self.job_client.job
        self.alist_client = storage_engine.get_storage_client(vm.session, self.job.engineId)
        self.create_time = time.time()
        self.finish = []
        self.doing = {}
        self.waiting = []
        self.last_watching = 0.0
        self.queue_num = 0
        self.scan_finish = False
        self.first_sync = None
        self.break_flag = False
        self.source_snapshot = {}
        self.source_scan_attempted = False
        self.source_scan_failed = False
        self.previous_source_snapshot = None
        self.source_snapshot_identity = source_snapshot_identity(self.job)
        self.current_tasks = {}
        self.moved_file_names = (
            load_moved_file_names(vm.session, self.job.id)
            if self.job.method == 2 else set()
        )
        self.sync_thread = threading.Thread(target=self.sync, name="job-sync-" + str(task_id), daemon=True)
        self.submit_thread = threading.Thread(target=self.task_submit, name="job-submit-" + str(task_id), daemon=True)

    def start(self):
        self.sync_thread.start()
        self.submit_thread.start()

    def get_current(self):
        self.last_watching = time.time()
        waits = [{
            "srcPath": w.src_path, "dstPath": w.dst_path, "isPath": 0,
            "fileName": w.file_name, "fileSize": w.file_size, "status": w.status,
            "type": w.copy_type, "progress": w.progress, "errMsg": w.err_msg,
            "createTime": w.create_time,
        } for w in self.waiting]
        dos = [{
            "srcPath": d.src_path, "dstPath": d.dst_path, "isPath": 0,
            "fileName": d.file_name, "fileSize": d.file_size, "status": d.status,
            "type": d.copy_type, "progress": d.progress, "errMsg": d.err_msg,
            "createTime": d.create_time,
        } for d in self.doing.values()]
        all_task = list(itertools.chain(waits, dos, self.finish))
        key_val_space = {
            "wait": 0, "running": 1, "success": 2, "fail": 7, "other": -1,
        }
        current_tasks = {}
        for val in key_val_space.values():
            current_tasks[val] = []
        otk = []
        otk_status = [3, 4, 5, 6, 8, 9]
        grouped = defaultdict(list)
        for task_item in all_task:
            grouped[task_item["status"]].append(task_item)
        for status, tasks in grouped.items():
            tasks.sort(key=lambda x: x["createTime"])
            if status in otk_status:
                otk.extend(tasks)
            else:
                current_tasks[status] = tasks
        current_tasks[-1] = otk
        self.current_tasks = current_tasks
        result = {
            "scanFinish": self.scan_finish,
            "doingTask": current_tasks[1],
            "createTime": int(self.create_time),
            "duration": int(self.last_watching - self.create_time),
            "firstSync": int(self.first_sync) if self.first_sync is not None else None,
            "num": {},
            "size": {},
        }
        for key, val in key_val_space.items():
            result["num"][key] = len(current_tasks[val])
            result["size"][key] = sum(
                item["fileSize"] for item in current_tasks[val]
                if item["fileSize"] is not None and item["type"] != 1
            )
        return result

    def get_current_by_status(self, status):
        return self.current_tasks[status]

    def task_submit(self):
        while True:
            if self.break_flag:
                break
            time.sleep(0.5)
            doing_nums = len(self.doing.keys())
            waiting_nums = len(self.waiting)
            if not self.scan_finish or doing_nums != 0 or waiting_nums != 0:
                while doing_nums < 20:
                    if self.break_flag:
                        break
                    if waiting_nums == 0:
                        break
                    if self.first_sync is None:
                        self.first_sync = time.time()
                    self.queue_num += 1
                    self.doing[self.queue_num] = self.waiting.pop(0)
                    self.doing[self.queue_num].doing_key = self.queue_num
                    self.doing[self.queue_num].do_by_thread()
                    doing_nums = len(self.doing.keys())
                    waiting_nums = len(self.waiting)
            else:
                break
        try_time = 0
        while len(self.doing.keys()) > 0:
            try_time += 1
            time.sleep(0.5)
            if try_time > 3:
                break
        try:
            if self.job.method == 2 and self._all_operations_successful():
                self.finalize_move()
            self.commit_source_snapshot()
            if self.finish:
                add_job_task_item_many(self.job_client.session, self.finish)
            self.update_task_status()
        finally:
            self.job_client.finish_run(self)

    def _all_operations_successful(self):
        return (not self.break_flag
                and self.source_scan_attempted
                and not self.source_scan_failed
                and all(item["status"] == 2 for item in self.finish))

    @staticmethod
    def normalize_root(path):
        path = str(path)
        return path if path.endswith("/") else path + "/"

    @staticmethod
    def entry_location(root_path, relative_path):
        root_path = JobTask.normalize_root(root_path)
        if "/" not in relative_path:
            return root_path, relative_path
        parent, name = relative_path.rsplit("/", 1)
        return root_path + parent + "/", name

    def finalize_move(self):
        fresh_source_directories = {}
        destination_roots = {
            self.normalize_root(item) for item in self.job.dst_path.split(":")
        }
        for entry in sorted(self.source_snapshot.values(), key=lambda item: item["path"]):
            if self.break_flag or entry["isDir"] or not self.file_size_allowed(entry["size"]):
                continue
            src_path, file_name = self.entry_location(self.job.src_path, entry["path"])
            matching = [item for item in self.finish
                        if item["type"] == 2 and item["srcPath"] == src_path and item["fileName"] == file_name]
            expected_destinations = {
                self.entry_location(root, entry["path"])[0] for root in destination_roots
            }
            delivered_destinations = {
                self.normalize_root(item["dstPath"]) for item in matching if item.get("dstPath")
            }
            if (len(matching) != len(delivered_destinations)
                    or delivered_destinations != expected_destinations):
                self.mark_move_delete_failure(
                    matching, src_path, file_name, entry["size"], G.move_delivery_incomplete
                )
                continue
            try:
                if src_path not in fresh_source_directories:
                    _entries, details = self.read_directory(src_path, 0, 0)
                    fresh_source_directories[src_path] = details
                fresh_entry = fresh_source_directories[src_path].get(file_name)
                fresh_size = None if fresh_entry is None else fresh_entry.get("size")
            except Exception as e:
                self.mark_move_delete_failure(matching, src_path, file_name, entry["size"], str(e))
                continue
            if fresh_entry is not None and (fresh_entry.get("isDir") or fresh_size != entry["size"]):
                self.mark_move_delete_failure(
                    matching, src_path, file_name, entry["size"], G.source_changed_during_move
                )
                continue
            if fresh_entry is None:
                if not matching:
                    self.copy_hook(src_path, None, file_name, entry["size"], status=2, copy_type=2)
                continue
            expected_fingerprint = entry.get("fingerprint")
            if expected_fingerprint is None:
                self.mark_move_delete_failure(
                    matching, src_path, file_name, entry["size"], G.source_version_unavailable
                )
                continue
            if fresh_entry.get("fingerprint") != expected_fingerprint:
                self.mark_move_delete_failure(
                    matching, src_path, file_name, entry["size"], G.source_changed_during_move
                )
                continue
            try:
                self.alist_client.delete_file(src_path, [file_name], 0)
            except Exception as e:
                err_msg = G.copy_success_but_delete_fail.format(str(e))
                self.mark_move_delete_failure(matching, src_path, file_name, entry["size"], err_msg)
            else:
                if not matching:
                    self.copy_hook(src_path, None, file_name, entry["size"], status=2, copy_type=2)
                append_moved_file(self.job_client.session, self.job.id, file_name, src_path=src_path)
                self.moved_file_names.add(file_name)

    def mark_move_delete_failure(self, matching, src_path, file_name, file_size, err_msg):
        if matching:
            for item in matching:
                item["status"] = 7
                item["errMsg"] = err_msg
        else:
            self.copy_hook(src_path, None, file_name, file_size, status=7,
                          err_msg=err_msg, copy_type=2)

    def commit_source_snapshot(self):
        if not self._all_operations_successful():
            return
        entries = list(self.source_snapshot.values())
        if self.job.method == 2:
            entries = [entry for entry in entries
                       if entry["isDir"] or not self.file_size_allowed(entry["size"])]
        try:
            expected_identity = getattr(
                self, "source_snapshot_identity", source_snapshot_identity(self.job)
            )
            replace_source_snapshot(
                self.job_client.session, self.job.id, entries, expected_identity=expected_identity
            )
        except Exception as e:
            logger.exception(e)
            self.copy_hook(self.normalize_root(self.job.src_path), None, None, None,
                          status=7, err_msg=str(e), is_path=1)

    def copy_hook(self, src_path, dst_path, name, size, alist_task_id=None, status=0,
                  err_msg=None, is_path=0, copy_type=0, create_time=int(time.time())):
        self.finish.append({
            "taskId": self.task_id,
            "srcPath": src_path,
            "dstPath": dst_path,
            "isPath": is_path,
            "fileName": name,
            "fileSize": size,
            "type": copy_type,
            "alistTaskId": alist_task_id,
            "status": status,
            "errMsg": err_msg,
            "createTime": create_time,
        })

    def del_hook(self, dst_path, name, size, status=2, err_msg=None, is_path=0, create_time=int(time.time())):
        self.finish.append({
            "taskId": self.task_id,
            "srcPath": None,
            "dstPath": dst_path,
            "isPath": is_path,
            "fileName": name,
            "fileSize": size,
            "type": 1,
            "alistTaskId": None,
            "status": status,
            "errMsg": err_msg,
            "createTime": create_time,
        })

    def sync(self):
        src_path = self.normalize_root(self.job.src_path)
        job_exclude = self.job.exclude
        spec = None
        if job_exclude is not None:
            spec = PathSpec.from_lines(GitWildMatchPattern, job_exclude.split(":"))
        dst_path_list = [self.normalize_root(item) for item in self.job.dst_path.split(":")]
        try:
            paths_overlap = getattr(self.alist_client, "paths_overlap", virtual_paths_overlap)
            if any(paths_overlap(src_path, dst_path) for dst_path in dst_path_list):
                raise ValueError(G.source_target_overlap)
            stored_snapshot = get_source_snapshot(self.job_client.session, self.job.id)
            if stored_snapshot["meta"]["initialized"] == 1:
                self.previous_source_snapshot = {
                    item["path"]: item for item in stored_snapshot["entries"]
                }
            else:
                self.previous_source_snapshot = None
            if self.job.source_mode == 1 and stored_snapshot["meta"]["initialized"] == 1:
                if self.scan_source_tree(src_path, spec, src_path):
                    self.sync_from_source_snapshot(stored_snapshot["entries"], dst_path_list)
            else:
                for index, dst_item in enumerate(dst_path_list):
                    self.sync_with_have(src_path, dst_item, spec, src_path, dst_item, index == 0)
        except Exception as e:
            logger.exception(e)
            self.source_scan_failed = True
            self.copy_hook(src_path, None, None, None, status=7, err_msg=str(e), is_path=1)
        finally:
            self.scan_finish = True

    def scan_source_tree(self, path, spec, root_path):
        if self.break_flag:
            return False
        try:
            entries = self.list_dir(path, True, spec, root_path)
        except Exception:
            return False
        for name in entries:
            if name.endswith("/") and not self.scan_source_tree(path + name, spec, root_path):
                return False
        return not self.break_flag and not self.source_scan_failed

    def sync_from_source_snapshot(self, stored_entries, dst_path_list):
        previous = {
            item["path"]: {
                "path": item["path"], "isDir": int(item["isDir"]),
                "size": item["size"], "fingerprint": item.get("fingerprint"),
            }
            for item in stored_entries
        }
        current = self.source_snapshot

        changed_files = [entry for path, entry in current.items()
                        if not entry["isDir"]
                        and self.file_size_allowed(entry["size"])
                        and (self.job.method == 2
                             or self.source_entry_changed(previous.get(path), entry))]
        new_directories = [entry for path, entry in current.items()
                          if entry["isDir"]
                          and (path not in previous or not previous[path]["isDir"])]

        removed = [entry for path, entry in previous.items()
                   if path not in current or current[path]["isDir"] != entry["isDir"]]

        for dst_root in dst_path_list:
            failed_directory_prefixes = []
            if self.job.method == 1:
                self.delete_snapshot_entries(dst_root, removed)

            for entry in sorted(new_directories, key=lambda item: (item["path"].count("/"), item["path"])):
                if any(self.path_within(entry["path"], prefix) for prefix in failed_directory_prefixes):
                    continue
                dst_path = dst_root + entry["path"] + "/"
                src_path = self.normalize_root(self.job.src_path) + entry["path"] + "/"
                status = 2
                err_msg = None
                try:
                    self.alist_client.mkdir(dst_path, self.job.scan_interval_t)
                except Exception as e:
                    status = 7
                    err_msg = str(e)
                    failed_directory_prefixes.append(entry["path"])
                self.copy_hook(src_path, dst_path, None, None, status=status, err_msg=err_msg, is_path=1)

            for entry in changed_files:
                parent_path, file_name = self.entry_location(dst_root, entry["path"])
                if any(self.path_within(entry["path"], prefix) for prefix in failed_directory_prefixes):
                    continue
                src_path, _ = self.entry_location(self.job.src_path, entry["path"])
                self.copy_file(src_path, parent_path, file_name, entry["size"])

    @staticmethod
    def path_within(path, prefix):
        return path == prefix or path.startswith(prefix + "/")

    @staticmethod
    def source_entry_changed(previous, current):
        if (previous is None or previous.get("isDir")
                or previous.get("size") != current.get("size")):
            return True
        previous_fingerprint = previous.get("fingerprint")
        current_fingerprint = current.get("fingerprint")
        return ((previous_fingerprint is not None or current_fingerprint is not None)
                and previous_fingerprint != current_fingerprint)

    def source_file_changed_since_snapshot(self, src_path, src_root_path, file_name):
        previous = getattr(self, "previous_source_snapshot", None)
        if previous is None:
            return False
        relative_base = (
            src_path[len(src_root_path):].strip("/")
            if src_path.startswith(src_root_path) else ""
        )
        relative_path = "/".join(item for item in (relative_base, file_name) if item)
        current = self.source_snapshot.get(relative_path)
        previous_entry = previous.get(relative_path)
        return (current is not None and previous_entry is not None
                and self.source_entry_changed(previous_entry, current))

    def delete_snapshot_entries(self, dst_root, removed_entries):
        for entry in removed_entries:
            if entry["isDir"] or not self.file_size_allowed(entry["size"]):
                continue
            parent_path, name = self.entry_location(dst_root, entry["path"])
            self.del_file(parent_path, name, entry["size"])

    def copy_file(self, src_path, dst_path, file_name, file_size):
        if self.break_flag:
            return
        if self.job.method == 2 and file_name in self.moved_file_names:
            logger.info(G.move_skipped_logged.format(fileName=file_name, srcPath=src_path))
            self.copy_hook(src_path, dst_path, file_name, file_size, status=2, copy_type=2,
                          err_msg=G.move_already_logged)
            return
        copy_item = CopyItem(src_path, dst_path, file_name, file_size, self.job.method, self)
        self.waiting.append(copy_item)

    def has_file_size_filter(self):
        return self.job.min_file_size is not None or self.job.max_file_size is not None

    def file_size_allowed(self, file_size):
        return is_file_size_allowed(file_size, self.job.min_file_size, self.job.max_file_size)

    def del_file(self, path, file_name, size):
        if self.break_flag:
            return
        is_path = file_name.endswith("/")
        status = 2
        err_msg = None
        create_time = int(time.time())
        try:
            self.alist_client.delete_file(
                path, [file_name if not is_path else file_name[:-1]], self.job.scan_interval_t
            )
        except Exception as e:
            status = 7
            err_msg = str(e)
        self.del_hook(path, file_name, None if is_path else size, status, err_msg, is_path, create_time)

    def list_dir(self, path, first_dst, spec, root_path, is_src=True):
        use_cache = 1 if is_src and not first_dst else getattr(self.job, f"use_cache_{'s' if is_src else 't'}")
        scan_interval = getattr(self.job, f"scan_interval_{'s' if is_src else 't'}")
        try:
            entries, details = self.read_directory(path, use_cache, scan_interval, spec, root_path)
            if is_src and first_dst:
                self.record_source_entries(path, root_path, entries, details)
            return entries
        except Exception as e:
            err_msg = G.scan_error.format(G.src if is_src else G.dst, str(e))
            logger.error(err_msg)
            logger.exception(e)
            if is_src and first_dst:
                self.source_scan_attempted = True
                self.source_scan_failed = True
            self.copy_hook(path if is_src else None, None if is_src else path, None, None,
                          status=7, err_msg=err_msg, is_path=1)
            raise e

    def read_directory(self, path, use_cache=0, scan_interval=0, spec=None, root_path=None):
        detail_api = getattr(self.alist_client, "file_list_detail_api", None)
        if callable(detail_api):
            raw_details = detail_api(path, use_cache, scan_interval, spec, root_path)
            details = {}
            entries = {}
            for name, raw_detail in raw_details.items():
                detail = raw_detail if isinstance(raw_detail, dict) else {}
                is_directory = bool(detail.get("isDir", name.endswith("/")))
                size = None if is_directory else detail.get("size")
                details[name] = {
                    "isDir": 1 if is_directory else 0,
                    "size": size,
                    "fingerprint": detail.get("fingerprint"),
                }
                entries[name] = {} if is_directory else size
            return entries, details

        entries = self.alist_client.file_list_api(path, use_cache, scan_interval, spec, root_path)
        details = {
            name: {
                "isDir": 1 if name.endswith("/") else 0,
                "size": None if name.endswith("/") else size,
                "fingerprint": None,
            }
            for name, size in entries.items()
        }
        return entries, details

    def record_source_entries(self, path, root_path, entries, details=None):
        self.source_scan_attempted = True
        relative_base = path[len(root_path):].strip("/") if path.startswith(root_path) else ""
        for name, size in entries.items():
            is_directory = name.endswith("/")
            clean_name = name[:-1] if is_directory else name
            relative_path = "/".join(item for item in (relative_base, clean_name) if item)
            entry = {
                "path": relative_path,
                "isDir": 1 if is_directory else 0,
                "size": None if is_directory else size,
            }
            fingerprint = (details or {}).get(name, {}).get("fingerprint")
            if fingerprint is not None:
                entry["fingerprint"] = fingerprint
            self.source_snapshot[relative_path] = entry

    def delete_target_only_dir(self, dst_path, spec, dst_root_path, first_dst):
        if self.break_flag:
            return
        try:
            dst_files = self.list_dir(dst_path, first_dst, spec, dst_root_path, False)
        except Exception:
            return
        for key, size in dst_files.items():
            if self.break_flag:
                return
            if key.endswith("/"):
                self.delete_target_only_dir(dst_path + key, spec, dst_root_path, first_dst)
            elif self.file_size_allowed(size):
                self.del_file(dst_path, key, size)

    def sync_with_have(self, src_path, dst_path, spec, src_root_path, dst_root_path, first_dst):
        if self.break_flag:
            return
        try:
            src_files = self.list_dir(src_path, first_dst, spec, src_root_path)
            dst_files = self.list_dir(dst_path, first_dst, spec, dst_root_path, False)
        except Exception:
            return
        for key in src_files.keys():
            if not key.endswith("/"):
                if not self.file_size_allowed(src_files[key]):
                    continue
                if (self.job.method == 2
                        or self.source_file_changed_since_snapshot(src_path, src_root_path, key)
                        or key not in dst_files or dst_files[key] != src_files[key]):
                    self.copy_file(src_path, dst_path, key, src_files[key])
            else:
                if key not in dst_files:
                    self.sync_with_out_have(src_path + key, dst_path + key, spec, src_root_path, dst_root_path, first_dst)
                else:
                    self.sync_with_have(src_path + key, dst_path + key, spec, src_root_path, dst_root_path, first_dst)
        if self.job.method == 1:
            for dst_key in dst_files.keys():
                if dst_key not in src_files:
                    if dst_key.endswith("/") and self.has_file_size_filter():
                        self.delete_target_only_dir(dst_path + dst_key, spec, dst_root_path, first_dst)
                    elif dst_key.endswith("/") or self.file_size_allowed(dst_files[dst_key]):
                        self.del_file(dst_path, dst_key, dst_files[dst_key])

    def sync_with_out_have(self, src_path, dst_path, spec, src_root_path, dst_root_path, first_dst):
        if self.break_flag:
            return
        status = 2
        err_msg = None
        try:
            self.alist_client.mkdir(dst_path, self.job.scan_interval_t)
        except Exception as e:
            status = 7
            err_msg = str(e)
        self.copy_hook(src_path, dst_path, None, None, status=status, err_msg=err_msg, is_path=1)
        if status != 2:
            return
        try:
            src_files = self.list_dir(src_path, first_dst, spec, src_root_path)
        except Exception:
            return
        for key in src_files.keys():
            if self.break_flag:
                break
            if key.endswith("/"):
                self.sync_with_out_have(src_path + key, dst_path + key, spec, src_root_path, dst_root_path, first_dst)
            elif self.file_size_allowed(src_files[key]):
                self.copy_file(src_path, dst_path, key, src_files[key])

    def update_task_status(self):
        self.get_current()
        fail_or_other_num = len(self.current_tasks[7]) + len(self.current_tasks[-1])
        status = 7 if self.break_flag else 2 if fail_or_other_num == 0 else 3
        self.job_client.persist_task_status(
            self.task_id, status, self.current_tasks, int(self.create_time)
        )
        # 归档一条同步记录（同步操作历史日志）。
        self._write_sync_record(status)

    def _write_sync_record(self, status: int) -> None:
        """把本次运行结果写入 sync_records 历史表，供审计 / 导出 / 过滤查询。"""
        try:
            from core.sync.job_dao import add_sync_record

            # 成功同步（移动）的文件数与数据量：finish 中 status==2 的明细。
            success_items = [it for it in self.finish if it.get("status") == 2]
            data_count = len(success_items)
            data_size = sum(int(it.get("fileSize") or 0) for it in success_items)

            # 错误信息：汇总失败/异常明细的 errMsg（最多取前若干条）。
            err_items = [
                it for it in self.finish
                if it.get("status") not in (2, None) and it.get("errMsg")
            ]
            err_msg = ""
            if err_items:
                err_msg = "；".join(
                    f"{it.get('fileName', '')}: {it.get('errMsg', '')}"
                    for it in err_items[:5]
                )
            if self.break_flag and not err_msg:
                err_msg = "任务被中止/中断"

            record = {
                "jobId": self.job.id,
                "jobName": getattr(self.job, "remark", "") or "",
                "operator": self.job_client.operator or "自动调度",
                "status": int(status),
                "dataCount": data_count,
                "dataSize": data_size,
                "errMsg": err_msg[:2000],
                "startTime": int(self.create_time),
                "endTime": int(time.time()),
                "createTime": int(time.time()),
            }
            add_sync_record(self.job_client.session, record)
        except Exception:
            logger.exception("写入同步记录失败（不影响主流程）")

    def finish_run(self):
        self.job_client.finish_run(self)


class JobClient:
    def __init__(self, job, session, is_init=False, session_factory=None):
        self.session = session
        # session_factory：供手动触发 / 定时调度在后台线程内重新开 session
        # 并重新加载 job，避免复用请求级 / 缓存里已 detached 的 SyncJob 实例。
        self.session_factory = session_factory
        self.job = job
        self.job_id = job.id
        self.scheduled = None
        self.scheduled_job = None
        self.job_doing = False
        self.run_lock = threading.Lock()
        self.current_job_task = None
        self.notify_hook = None
        # operator：本次运行的操作人员 / 触发来源（手动 / 自动调度 / system）。
        self.operator = "自动调度"
        try:
            self.do_by_time()
        except Exception as e:
            if is_init:
                logger.error(G.del_job_course_error.format(json.dumps({
                    "id": getattr(self.job, "id", None), "srcPath": self.job.src_path,
                    "dstPath": self.job.dst_path,
                }, ensure_ascii=False)))
                try:
                    delete_job(self.session, self.job_id)
                except Exception:
                    pass
            raise e

    def persist_task_status(self, task_id, status, task_list, create_time):
        """落库任务状态 + 统计 taskNum，并触发通知（若已装配）。"""
        from core.sync.job_dao import update_job_task_status, update_job_task_num_many

        duration = int(time.time() - create_time) if create_time else None
        hours, minutes, seconds = convert_seconds(duration)
        duration_text = f"{hours}时{minutes}分{seconds}秒"
        sum_size = sum(
            item["fileSize"] for item in task_list.get(2, [])
            if item["fileSize"] is not None
        )
        size_text = convert_bytes(sum_size)
        task_num = {
            "waitNum": 0, "runningNum": 0,
            "successNum": len(task_list.get(2, [])),
            "failNum": len(task_list.get(7, [])),
            "otherNum": len(task_list.get(-1, [])),
            "allNum": len(task_list.get(2, [])) + len(task_list.get(7, [])) + len(task_list.get(-1, [])),
            "duration": duration,
            "sumSize": sum_size,
        }
        update_job_task_status(self.session, task_id, status)
        update_job_task_num_many(self.session, [{
            "taskId": task_id, "taskNum": json.dumps(task_num, ensure_ascii=False),
        }])
        if self.notify_hook is not None:
            try:
                job = get_job_by_id(self.session, self.job_id)
                self.notify_hook(job, status, task_num, duration_text, size_text)
            except Exception:
                logger.exception("同步任务结束通知失败")

    def _reload_job(self, session):
        """后台线程入口：用本线程 session 重新加载 job，确保 bound。"""
        from core.sync.job_dao import get_job_by_id

        self.session = session
        self.job = get_job_by_id(session, self.job_id)

    def _ensure_job_bound(self):
        """兜底：若 self.job 未绑定当前 session，重新加载。"""
        if self.job is None or self.job not in self.session:
            from core.sync.job_dao import get_job_by_id

            self.job = get_job_by_id(self.session, self.job_id)

    def do_job(self, lock_acquired=False, operator=None):
        if not lock_acquired and not self.run_lock.acquire(blocking=False):
            return
        if operator:
            self.operator = operator
        self.job_doing = True

        # 优先走「后台线程内自有 session」路径：手动触发与定时调度共用，
        # 保证 SyncJob 在执行期间始终 bound 到存活 session，杜绝
        # "Instance is not bound to a Session" 的 attribute refresh 报错。
        if self.session_factory is not None:
            with self.session_factory() as session:
                self._reload_job(session)
                self._run_job_inner()
            return

        # 兜底（无 factory 时）：依赖调用方已保证 session 存活。
        self._ensure_job_bound()
        self._run_job_inner()

    def _run_job_inner(self):
        """在「已绑定 session」的上下文中执行一次同步（线程安全边界）。"""
        task_id = None
        try:
            task_id = add_job_task(self.session, {
                "jobId": self.job_id,
                "runTime": int(time.time()),
            })
            if self.job.enable == 0:
                raise Exception("abort")
            task = JobTask(task_id, self)
            self.current_job_task = task
            task.start()
            # 阻塞等待后台 sync / submit 子线程结束，确保 self.session 的
            # 生命周期完整覆盖整个扫描 + 复制 + 落库过程（子线程均使用 self.session）。
            task.sync_thread.join()
            task.submit_thread.join()
            self.session.commit()
        except Exception as e:
            self.finish_run()
            err_msg = G.do_job_err.format(str(e))
            logger.error(err_msg)
            if task_id is not None:
                update_job_task_status(self.session, task_id, 6, err_msg)
                try:
                    self.session.commit()
                except Exception:
                    pass
            logger.exception(e)

    def do_manual(self, operator="手动", session_factory=None):
        if not self.run_lock.acquire(blocking=False):
            raise Exception(G.job_running)
        self.operator = operator or "手动"
        self.job_doing = True
        sf = session_factory or self.session_factory

        def _run():
            if sf is not None:
                with sf() as session:
                    self._reload_job(session)
                    self.do_job(lock_acquired=True, operator=self.operator)
            else:
                # 兜底：无 factory 时直接跑（调用方需保证传入的 session 存活）。
                self.do_job(lock_acquired=True, operator=self.operator)

        do_job_thread = threading.Thread(
            target=_run, name="job-manual-" + str(self.job_id), daemon=True
        )
        do_job_thread.start()

    def finish_run(self, task=None):
        if task is None or self.current_job_task is task:
            self.current_job_task = None
        self.job_doing = False
        if self.run_lock.locked():
            try:
                self.run_lock.release()
            except RuntimeError:
                pass

    def do_by_time(self):
        params = {
            "func": self.do_job,
            "misfire_grace_time": 15 * 60,
            "trigger": "interval" if self.job.is_cron == 0 else "cron",
        }
        if self.job.is_cron == 0:
            interval = self.job.interval
            if interval is not None and str(interval).strip() != "":
                params["minutes"] = interval
            else:
                raise Exception(G.interval_lost)
        elif self.job.is_cron == 1:
            flag = 0
            for item in ["year", "month", "day", "week", "day_of_week", "hour", "minute", "second",
                         "start_date", "end_date"]:
                value = getattr(self.job, item, None)
                if value is not None and str(value) != "":
                    flag += 1
                    params[item] = value
            if flag == 0:
                raise Exception(G.cron_lost)
        else:
            return
        self.scheduled = BackgroundScheduler()
        self.scheduled_job = self.scheduled.add_job(**params)
        self.scheduled.start()
        if self.job.enable == 0:
            self.scheduled_job.pause()

    def resume_job(self):
        if self.scheduled_job is None:
            raise Exception(G.cannot_resume_lost_job)
        update_job_enable(self.session, self.job_id, 1)
        self.job.enable = 1
        self.scheduled_job.resume()

    def abort_job(self):
        if self.current_job_task:
            self.current_job_task.break_flag = True

    def stop_job(self, remove=False):
        self.job.enable = 0
        if self.current_job_task:
            self.current_job_task.break_flag = True
        if remove:
            if self.scheduled is not None:
                try:
                    self.scheduled.shutdown(wait=False)
                except Exception as e:
                    logger.warning(G.stop_fail.format(str(e)))
                    logger.exception(e)
                self.scheduled = None
        else:
            if self.scheduled_job is not None:
                try:
                    self.scheduled_job.pause()
                except Exception as e:
                    logger.warning(G.disable_fail.format(str(e)))
                    logger.exception(e)
        if not remove:
            update_job_enable(self.session, self.job_id, 0)
            update_job_task_status_by_status_and_job_id(self.session, self.job_id)


def add_job_task_item_many(session, items):
    from core.sync.job_dao import add_job_task_item_many as _impl
    _impl(session, items)


def update_job_task_status_by_status_and_job_id(session, job_id):
    from core.sync.job_dao import update_job_task_status_by_status_and_job_id as _impl
    _impl(session, job_id)


def delete_job(session, job_id):
    from core.sync.job_dao import delete_job as _impl
    _impl(session, job_id)


def update_job_enable(session, job_id, enable):
    from core.sync.job_dao import update_job_enable as _impl
    _impl(session, job_id)
