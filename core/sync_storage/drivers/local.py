"""本地目录驱动（移植自 TaoSync service/storage/drivers/local.py）。

后端为进程可见的绝对路径；Docker 部署需先把宿主机目录挂进容器。
"""
from __future__ import annotations

import os
import shutil

from core.sync_storage.alist_compat import fileFingerprint
from core.sync_storage.base import (
    TransferCancelled,
    check_cancel,
    child_path,
    normalize_path,
)
from core.sync_storage.base import StorageDriver


def _list_dir(path):
    entries = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        # 符号链接一律当作普通文件处理，不递归进入目录型符号链接，
        # 避免符号链接循环（指向自身/父目录）或跨挂载遍历导致扫描无限进行。
        if os.path.islink(full):
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                size = 0
                mtime = None
            entries.append({
                "name": name,
                "is_dir": False,
                "size": size,
                "fingerprint": fileFingerprint("local", size, mtime),
            })
            continue
        if os.path.isdir(full):
            entries.append({"name": name, "is_dir": True, "size": None})
        else:
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                size = 0
                mtime = None
            entries.append({
                "name": name,
                "is_dir": False,
                "size": size,
                "fingerprint": fileFingerprint("local", size, mtime),
            })
    return entries


class LocalDriver(StorageDriver):
    driver_type = "local"

    def __init__(self, config):
        root = str(config.get("root_path") or "").strip()
        if not root:
            raise ValueError("local root_path is required")
        if not os.path.isabs(root):
            raise ValueError("local root_path must be an absolute path")
        self.root = os.path.abspath(root)

    # ---- 虚拟路径 <-> 本地路径 ----
    def _to_local(self, path):
        rel = normalize_path(path)
        if rel == "/":
            return self.root
        # 防穿越：join 后必须仍在 root 内
        full = os.path.normpath(os.path.join(self.root, rel.lstrip("/")))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError("path escapes storage root")
        return full

    def list(self, path, details=False):
        local = self._to_local(path)
        if not os.path.isdir(local):
            raise FileNotFoundError(local)
        if details:
            return _list_dir(local)
        return [e["name"] for e in _list_dir(local)]

    def mkdir(self, path):
        os.makedirs(self._to_local(path), exist_ok=True)

    def delete(self, path):
        local = self._to_local(path)
        if local == self.root:
            raise ValueError("storage root cannot be deleted")
        if os.path.isdir(local) and not os.path.islink(local):
            shutil.rmtree(local)
        elif os.path.exists(local):
            os.remove(local)

    def download(self, path, target, progress=None, cancel=None):
        check_cancel(cancel)
        source = self._to_local(path)
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        total = os.path.getsize(source)
        copied = 0
        with open(source, "rb") as fh:
            while True:
                check_cancel(cancel)
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                copied += len(chunk)
                if progress is not None and total:
                    progress(copied / total)
        if progress is not None:
            progress(1.0)

    def upload(self, path, source, size=None, progress=None, cancel=None):
        check_cancel(cancel)
        local = self._to_local(path)
        parent = os.path.dirname(local)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = local + ".taosync.tmp"
        copied = 0
        src_file = source if hasattr(source, "read") else open(source, "rb")
        try:
            with open(tmp, "wb") as out:
                while True:
                    check_cancel(cancel)
                    chunk = src_file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    copied += len(chunk)
                    if progress is not None and size:
                        progress(copied / size)
            os.replace(tmp, local)
        finally:
            if not hasattr(source, "read"):
                src_file.close()
        if progress is not None:
            progress(1.0)

    def copy(self, source_path, destination_path, size=None, progress=None, cancel=None):
        """Local-to-local native copy (no streaming through process)."""
        check_cancel(cancel)
        src = self._to_local(source_path)
        dst = self._to_local(destination_path)
        parent = os.path.dirname(dst)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = dst + ".taosync.tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        if progress is not None:
            progress(1.0)
