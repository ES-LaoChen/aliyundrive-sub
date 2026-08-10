"""虚拟路径 / 挂载路径重叠判定（移植自 TaoSync service/storage/pathIdentity.py）。

范围收敛：仅保留 ``local`` 后端的物理路径重叠判定，以及虚拟路径重叠判定。
外部 AList 引擎的后端语义未知，统一走保守的虚拟路径重叠检查（case 不敏感）。
"""
from __future__ import annotations

import os
import posixpath

from core.sync_storage.base import normalize_path


def virtual_paths_overlap(first_path, second_path, case_sensitive=True):
    first = normalize_path(first_path)
    second = normalize_path(second_path)
    if not case_sensitive:
        first = first.casefold()
        second = second.casefold()
    return _path_overlaps(first, second)


def _mount_lookup(mounts):
    rows = mounts.values() if isinstance(mounts, dict) else mounts
    return {str(row["name"]): row for row in rows}


def _resolve_mount(mounts, path):
    normalized = normalize_path(path, allow_root=False)
    parts = normalized.strip("/").split("/")
    mount = mounts.get(parts[0])
    if mount is None:
        return None, None
    relative = "/" + "/".join(parts[1:]) if len(parts) > 1 else "/"
    return mount, relative


def _path_overlaps(first, second, separator="/"):
    first = first.rstrip(separator) or separator
    second = second.rstrip(separator) or separator
    return (
        first == second
        or first.startswith(second.rstrip(separator) + separator)
        or second.startswith(first.rstrip(separator) + separator)
    )


def _local_path(config, relative):
    root = config.get("root_path") or config.get("path")
    if not root:
        return None
    root = os.path.realpath(os.path.abspath(os.path.expanduser(str(root))))
    parts = normalize_path(relative).strip("/").split("/") if relative != "/" else []
    return os.path.normcase(os.path.realpath(os.path.join(root, *parts)))


def _local_paths_overlap(first_config, first_relative, second_config, second_relative):
    first = _local_path(first_config, first_relative)
    second = _local_path(second_config, second_relative)
    if first is None or second is None:
        return False
    try:
        common = os.path.commonpath((first, second))
    except ValueError:
        return False
    return common == first or common == second


def mount_paths_overlap(mounts, first_path, second_path):
    """Return whether two TaoSync paths may address overlapping backend data."""
    lookup = _mount_lookup(mounts)
    first_mount, first_relative = _resolve_mount(lookup, first_path)
    second_mount, second_relative = _resolve_mount(lookup, second_path)
    if first_mount is None or second_mount is None:
        return False
    first_type = str(first_mount.get("driverType") or "").strip().lower()
    second_type = str(second_mount.get("driverType") or "").strip().lower()
    if first_type != second_type:
        return False
    first_config = first_mount.get("config") or {}
    second_config = second_mount.get("config") or {}
    if first_type == "local":
        return _local_paths_overlap(
            first_config, first_relative, second_config, second_relative
        )
    return False
