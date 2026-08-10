"""同步作业辅助函数（移植自 TaoSync jobClient.py / jobService.py 顶部工具函数）。"""
from __future__ import annotations

import posixpath

MAX_SQLITE_INTEGER = 9223372036854775807


def is_file_size_allowed(file_size, min_file_size=None, max_file_size=None):
    if min_file_size is not None and file_size < min_file_size:
        return False
    if max_file_size is not None and file_size > max_file_size:
        return False
    return True


def normalize_file_size(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise Exception("文件大小无效")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise Exception("文件大小无效")
    if result < 0 or result > MAX_SQLITE_INTEGER:
        raise Exception("文件大小无效")
    return result


def normalize_source_mode(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value in ('0', '1'):
        return int(value)
    raise Exception("source mode 无效")


def normalize_virtual_path(path):
    value = str(path).replace('\\', '/')
    return posixpath.normpath('/' + value.lstrip('/')).casefold()


def virtual_paths_overlap(first_path, second_path):
    first = normalize_virtual_path(first_path)
    second = normalize_virtual_path(second_path)
    return (first == second
            or first.startswith(second.rstrip('/') + '/')
            or second.startswith(first.rstrip('/') + '/'))
