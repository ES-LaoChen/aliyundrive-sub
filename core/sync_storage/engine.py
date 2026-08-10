"""同步存储引擎（移植自 TaoSync engineService / storageService / taosyncClient）。

把源项目三块逻辑整合到当前项目的 SQLAlchemy + 会话工厂体系中：
- 存储目录（挂载）DAO：``storage_mount`` 表的增删改查 + token 旋转合并。
- 同步引擎（alist 表）DAO：内置 ``taosync`` 与外部 OpenList/AList。
- ``TaoSyncClient``：AList 兼容外观，把本地挂载驱动 + 外部 AList 统一为
  ``fileListApi / copyFile / mkdir / deleteFile`` 等接口，供 job 引擎调用。

后端范围：local（真实驱动）+ 外部 OpenList/AList（HTTP facade）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid

from core.sync_storage.alist_compat import AlistClient
from core.sync_storage.base import (
    TransferCancelled,
    check_cancel,
    child_path,
    normalize_path,
    stream_transfer,
)
from core.sync_storage.factory import SECRET_FIELDS, createDriver, getDriverTypes
from core.sync_storage.pathIdentity import mount_paths_overlap, virtual_paths_overlap
from db import get_session_local

from models_sync import SyncEngine, SyncStorageMount

MOUNT_NAME_PATTERN = re.compile(r"^[^\\/:\x00-\x1f]+$")

# ── 进程内客户端缓存（与 TaoSync engineService 同语义）──
_client_lock = threading.Lock()
_client_list = {}
_engine_locks = {}
_mount_refresh_locks = {}
_mount_refresh_locks_guard = threading.Lock()


# =========================================================================
# DAO：同步引擎（alist 表）
# =========================================================================
class SyncEngineDAO:
    """``alist`` 表的 CRUD（用会话工厂）。"""

    def __init__(self, session_factory):
        self._sf = session_factory

    def get_engine_by_id(self, engine_id):
        with self._sf() as db:
            row = db.get(SyncEngine, int(engine_id))
            return _row_to_dict(row) if row else None

    def get_engine_list(self):
        with self._sf() as db:
            rows = db.query(SyncEngine).order_by(SyncEngine.id.asc()).all()
            return [_row_to_dict(r) for r in rows]

    def add_engine(self, engine):
        with self._sf() as db:
            obj = SyncEngine(
                remark=engine.get("remark") or "",
                url=engine.get("url") or "",
                userName=engine.get("userName") or "",
                token=engine.get("token") or "",
                engineType=engine.get("engineType") or "taosync",
                systemKey=engine.get("systemKey") or "taosync",
                protected=int(engine.get("protected") or 0),
                createTime=int(time.time()),
            )
            db.add(obj)
            db.commit()
            db.refresh(obj)
            return obj.id

    def update_engine(self, engine):
        with self._sf() as db:
            obj = db.get(SyncEngine, int(engine["id"]))
            if obj is None:
                raise Exception("同步引擎不存在")
            if obj.protected == 1:
                raise Exception("内置引擎受保护，不可编辑")
            if engine.get("remark") is not None and engine["remark"].strip() == "":
                obj.remark = None
            elif "remark" in engine:
                obj.remark = engine["remark"]
            if "token" in engine:
                if engine["token"] is None:
                    pass
                else:
                    obj.token = engine["token"].strip()
            if "url" in engine:
                url = engine["url"]
                if url.endswith("/"):
                    url = url[:-1]
                obj.url = url
            db.commit()

    def remove_engine(self, engine_id):
        with self._sf() as db:
            obj = db.get(SyncEngine, int(engine_id))
            if obj is None:
                raise Exception("同步引擎不存在")
            if obj.protected == 1:
                raise Exception("内置引擎受保护，不可删除")
            db.delete(obj)
            db.commit()


# =========================================================================
# DAO：存储目录（storage_mount 表）
# =========================================================================
class StorageMountDAO:
    """``storage_mount`` 表的 CRUD（用会话工厂）。"""

    def __init__(self, session_factory):
        self._sf = session_factory

    def get_mount_list(self, engine_id):
        with self._sf() as db:
            rows = (
                db.query(SyncStorageMount)
                .filter_by(engineId=int(engine_id), enabled=1)
                .order_by(SyncStorageMount.createTime.asc(), SyncStorageMount.id.asc())
                .all()
            )
            return [_mount_to_dict(r) for r in rows]

    def get_mount_by_id(self, mount_id):
        with self._sf() as db:
            row = db.get(SyncStorageMount, int(mount_id))
            if row is None:
                raise Exception("storage mount not found")
            return _mount_to_dict(row)

    def add_mount(self, mount):
        with self._sf() as db:
            obj = SyncStorageMount(
                engineId=int(mount["engineId"]),
                name=mount["name"],
                driverType=mount.get("driverType") or "local",
                config=json.dumps(mount.get("config") or {}, ensure_ascii=False),
                enabled=int(mount.get("enabled", 1)),
                authVersion=int(mount.get("authVersion", 1)),
                configVersion=1,
                createTime=int(time.time()),
            )
            db.add(obj)
            db.commit()
            db.refresh(obj)
            return obj.id

    def update_mount(self, mount):
        with self._sf() as db:
            obj = db.get(SyncStorageMount, int(mount["id"]))
            if obj is None:
                raise Exception("storage mount not found")
            obj.name = mount["name"]
            obj.driverType = mount.get("driverType") or obj.driverType
            obj.config = json.dumps(mount.get("config") or {}, ensure_ascii=False)
            obj.enabled = int(mount.get("enabled", 1))
            if "authVersion" in mount:
                obj.authVersion = int(mount["authVersion"])
            obj.configVersion = (obj.configVersion or 0) + 1
            db.commit()

    def update_mount_tokens(self, mount_id, expected_auth_version, token_values,
                            expected_tokens=None):
        """Merge rotated tokens only if the originating config is current."""
        for _ in range(3):
            with self._sf() as db:
                obj = db.get(SyncStorageMount, int(mount_id))
                if obj is None:
                    return None
                if int(obj.authVersion) != int(expected_auth_version):
                    return None
                try:
                    config = json.loads(obj.config or "{}")
                except (TypeError, ValueError):
                    config = {}
                if expected_tokens and any(
                    config.get(k) != v for k, v in expected_tokens.items()
                ):
                    return {"conflict": True, "config": config}
                config.update(token_values)
                obj.config = json.dumps(config, ensure_ascii=False)
                obj.configVersion = (obj.configVersion or 0) + 1
                db.commit()
                return True
        return None

    def remove_mount(self, mount_id):
        with self._sf() as db:
            obj = db.get(SyncStorageMount, int(mount_id))
            if obj is None:
                return
            db.delete(obj)
            db.commit()


# =========================================================================
# 序列化辅助
# =========================================================================
def _row_to_dict(row):
    d = {}
    for col in row.__table__.columns:
        d[col.name] = getattr(row, col.name)
    return d


def _mount_to_dict(row):
    d = _row_to_dict(row)
    try:
        d["config"] = json.loads(row.config or "{}")
    except (TypeError, ValueError):
        d["config"] = {}
    return d


# =========================================================================
# 内置 TaoSync 引擎（local 挂载）的存储目录服务
# =========================================================================
def _get_mount_refresh_lock(mount_id, auth_version):
    key = (int(mount_id), int(auth_version))
    with _mount_refresh_locks_guard:
        return _mount_refresh_locks.setdefault(key, threading.Lock())


def _sanitized(row):
    row = dict(row)
    config = dict(row.get("config") or {})
    secret_state = {}
    for field in SECRET_FIELDS.get(row["driverType"], set()):
        secret_state[field] = bool(config.get(field))
        config[field] = ""
    row["config"] = config
    row["secretState"] = secret_state
    return row


def _require_taosync(engine):
    if engine.get("engineType") != "taosync" or engine.get("systemKey") != "taosync":
        raise ValueError("存储目录只能在内置 TaoSync 引擎下管理")


def _clean_name(name):
    name = str(name or "").strip()
    if not name or name in (".", "..") or not MOUNT_NAME_PATTERN.match(name):
        raise ValueError("无效的存储目录名称")
    return name


class StorageService:
    """存储目录（挂载）管理（仅限内置 taosync 引擎，后端为 local）。"""

    def __init__(self, session_factory):
        self._sf = session_factory
        self._mount_dao = StorageMountDAO(session_factory)
        self._engine_dao = SyncEngineDAO(session_factory)

    def get_mount_list(self, engine_id):
        _require_taosync(self._engine_dao.get_engine_by_id(engine_id))
        return [_sanitized(r) for r in self._mount_dao.get_mount_list(engine_id)]

    def get_supported_drivers(self):
        return getDriverTypes()

    def _validate_unique_name(self, engine_id, name, exclude_id=None):
        for row in self._mount_dao.get_mount_list(engine_id):
            if row["name"].casefold() == name.casefold() and row["id"] != exclude_id:
                raise ValueError("已存在同名的存储目录")

    def _normalize_driver_config(self, driver_type, config):
        driver = createDriver(driver_type, config)
        if driver_type == "local":
            driver.list("/")
        return dict(getattr(driver, "config", config))

    def _normalize_driver_config_safe(self, driver_type, config):
        """同 _normalize_driver_config，但把底层驱动的校验异常转译为清晰中文提示。"""
        try:
            return self._normalize_driver_config(driver_type, config)
        except ValueError as e:
            msg = str(e)
            if "root_path" in msg:
                raise ValueError("请填写「根目录绝对路径」（例如 /data/media 或 D:\\media）")
            raise

    def add_mount(self, data):
        engine_id = int(data["engineId"])
        _require_taosync(self._engine_dao.get_engine_by_id(engine_id))
        name = _clean_name(data.get("name"))
        driver_type = str(data.get("driverType") or "").strip().lower()
        raw_config = data.get("config") or {}
        if not isinstance(raw_config, dict):
            raise ValueError("存储配置必须是一个对象")
        config = dict(raw_config)
        self._validate_unique_name(engine_id, name)
        config = self._normalize_driver_config_safe(driver_type, config)
        mount_id = self._mount_dao.add_mount({
            "engineId": engine_id,
            "name": name,
            "driverType": driver_type,
            "config": config,
            "enabled": 1,
        })
        _invalidate_engine(engine_id)
        return mount_id

    def update_mount(self, data):
        mount_id = int(data["id"])
        old = self._mount_dao.get_mount_by_id(mount_id)
        _require_taosync(self._engine_dao.get_engine_by_id(old["engineId"]))
        name = _clean_name(data.get("name", old["name"]))
        if name != old["name"]:
            raise ValueError("存储目录名称不可修改，请删除后重建")
        driver_type = str(data.get("driverType") or old["driverType"]).strip().lower()
        if driver_type != old["driverType"]:
            raise ValueError("存储驱动类型不可修改")
        raw_config = data.get("config") or {}
        if not isinstance(raw_config, dict):
            raise ValueError("存储配置必须是一个对象")
        config = {**old["config"], **dict(raw_config)}
        config = self._normalize_driver_config_safe(driver_type, config)
        self._mount_dao.update_mount({
            "id": mount_id,
            "name": name,
            "driverType": driver_type,
            "config": config,
            "enabled": int(data.get("enabled", old.get("enabled", 1))),
            "authVersion": int(old.get("authVersion", 1)),
        })
        _invalidate_engine(old["engineId"])

    def update_mount_config(self, mount_id, expected_auth_version, config,
                            expected_tokens=None):
        token_fields = {"access_token", "refresh_token", "expires_at", "drive_id"}
        values = {k: config.get(k) for k in token_fields if k in config}
        return self._mount_dao.update_mount_tokens(
            int(mount_id), int(expected_auth_version), values,
            expected_tokens=expected_tokens,
        )

    def remove_mount(self, mount_id):
        old = self._mount_dao.get_mount_by_id(int(mount_id))
        _require_taosync(self._engine_dao.get_engine_by_id(old["engineId"]))
        self._mount_dao.remove_mount(int(mount_id))
        _invalidate_engine(old["engineId"])


# =========================================================================
# TaoSyncClient：AList 兼容外观
# =========================================================================
class _CopyTask:
    def __init__(self, client, source_path, destination_path, delete_source=False):
        self.client = client
        self.id = uuid.uuid4().hex
        self.source_path = source_path
        self.destination_path = destination_path
        self.delete_source = delete_source
        self.state = 0
        self.progress = 0.0
        self.error = ""
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.delete_when_done = False

    def start(self):
        self.thread = threading.Thread(
            target=self._run, name="taosync-copy-" + self.id[:8], daemon=True)
        self.thread.start()

    def _report(self, value):
        with self.lock:
            self.progress = round(max(0.0, min(1.0, float(value))) * 100, 2)

    def _run(self):
        with self.lock:
            self.state = 1
        try:
            if self.source_path == self.destination_path:
                with self.lock:
                    self.progress = 100.0
                    self.state = 2
                return
            source_mount, source_relative = self.client.resolve(self.source_path)
            destination_mount, destination_relative = self.client.resolve(
                self.destination_path)
            size = self.client.fileSize(source_mount, source_relative)
            source_driver = self.client.getDriver(source_mount)
            if source_mount["id"] == destination_mount["id"]:
                source_driver.copy(
                    source_relative, destination_relative,
                    size=size, progress=self._report, cancel=self.cancel_event,
                )
            else:
                destination_driver = self.client.getDriver(destination_mount)
                stream_transfer(
                    source_driver, source_relative,
                    destination_driver, destination_relative,
                    size, self._report, self.cancel_event,
                )
            self.client._invalidateMountCache(destination_mount["id"])
            if self.delete_source:
                if self.cancel_event.is_set():
                    with self.lock:
                        self.progress = 100.0
                        self.state = 2
                    return
                source_driver.delete(source_relative)
                self.client._invalidateMountCache(source_mount["id"])
            with self.lock:
                self.progress = 100.0
                self.state = 2
        except TransferCancelled:
            with self.lock:
                self.state = 4
                self.error = "transfer cancelled"
        except Exception as exc:
            with self.lock:
                self.state = 7
                self.error = str(exc)
        finally:
            with self.lock:
                delete_when_done = self.delete_when_done
            if delete_when_done:
                self.client._forgetTask(self.id, self)

    def info(self):
        with self.lock:
            return {
                "id": self.id,
                "name": "copy {} to {}".format(self.source_path, self.destination_path),
                "state": self.state,
                "status": "running" if self.state == 1 else "finished",
                "progress": self.progress,
                "error": self.error,
            }

    def cancel(self):
        self.cancel_event.set()

    def deleteAfterFinish(self):
        with self.lock:
            self.delete_when_done = True
            terminal = self.state in (2, 4, 7)
        if terminal:
            self.client._forgetTask(self.id, self)


class TaoSyncClient:
    """AList-compatible facade over TaoSync's internal storage drivers (local)."""

    def __init__(self, engine_id, mount_dao: StorageMountDAO,
                 storage_service: StorageService):
        self.alistId = int(engine_id)
        self.user = "TaoSync"
        self.waits = {}
        self.tasks = {}
        self.tasks_lock = threading.Lock()
        self.mounts = {}
        self.entry_cache = {}
        self.entry_cache_lock = threading.Lock()
        self.cache_ttl = 15.0
        self._mount_dao = mount_dao
        self._storage_service = storage_service
        for row in mount_dao.get_mount_list(self.alistId):
            mount_id = int(row["id"])
            auth_version = int(row.get("authVersion") or 1)

            def save_config(config, expected_tokens=None, current_id=mount_id,
                            current_auth_version=auth_version):
                return storage_service.update_mount_config(
                    current_id, current_auth_version, config,
                    expectedTokens=expected_tokens,
                )

            def load_config(current_id=mount_id):
                return mount_dao.get_mount_by_id(current_id)

            row = dict(row)
            try:
                row["driver"] = createDriver(
                    row["driverType"], row["config"],
                    save_config=save_config, load_config=load_config,
                    refresh_lock=_get_mount_refresh_lock(mount_id, auth_version),
                    auth_version=auth_version,
                )
                row["driverError"] = ""
            except Exception as exc:
                row["driver"] = None
                row["driverError"] = str(exc)
            self.mounts[row["name"]] = row

    def updateAlistId(self, alistId):
        self.alistId = int(alistId)

    def mountPathsOverlap(self, firstPath, secondPath):
        return mount_paths_overlap(self.mounts, firstPath, secondPath)

    def pathsOverlap(self, firstPath, secondPath):
        return (
            virtual_paths_overlap(firstPath, secondPath, case_sensitive=True)
            or self.mountPathsOverlap(firstPath, secondPath)
        )

    def checkWait(self, path, scanInterval=0):
        if not scanInterval:
            return
        normalized = normalize_path(path)
        root = normalized.strip("/").split("/", 1)[0] if normalized != "/" else "/"
        if root in self.waits:
            elapsed = time.time() - self.waits[root]
            if elapsed < scanInterval:
                time.sleep(scanInterval - elapsed)
        self.waits[root] = time.time()

    def resolve(self, path):
        normalized = normalize_path(path, allow_root=False)
        parts = normalized.strip("/").split("/")
        mount = self.mounts.get(parts[0])
        if mount is None:
            raise FileNotFoundError("storage directory not found: " + parts[0])
        relative = "/" + "/".join(parts[1:]) if len(parts) > 1 else "/"
        return mount, relative

    @staticmethod
    def getDriver(mount):
        driver = mount.get("driver")
        if driver is None:
            detail = mount.get("driverError") or "driver could not be initialized"
            raise RuntimeError(
                "存储目录 '{}' 不可用：{}".format(mount.get("name", ""), detail))
        return driver

    def _invalidateMountCache(self, mount_id=None):
        with self.entry_cache_lock:
            if mount_id is None:
                self.entry_cache.clear()
            else:
                for key in [key for key in self.entry_cache if key[0] == mount_id]:
                    self.entry_cache.pop(key, None)

    def _entries(self, path, useCache=0):
        normalized = normalize_path(path)
        if normalized == "/":
            return [{"name": name, "is_dir": True, "size": None}
                    for name in sorted(self.mounts)]
        mount, relative = self.resolve(normalized)
        cache_key = (mount["id"], relative)
        if int(useCache or 0) == 1:
            with self.entry_cache_lock:
                cached = self.entry_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.cache_ttl:
                return [dict(item) for item in cached[1]]
        entries = self.getDriver(mount).list(relative, details=True)
        with self.entry_cache_lock:
            self.entry_cache[cache_key] = (time.monotonic(), [dict(item) for item in entries])
        return entries

    def fileListApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        details = self.fileListDetailApi(path, useCache, scanInterval, spec, rootPath)
        return {
            name: {} if detail['isDir'] else detail['size']
            for name, detail in details.items()
        }

    def fileListDetailApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        self.checkWait(path, scanInterval)
        entries = self._entries(path, useCache)
        result = {
            item["name"] + "/" if item["is_dir"] else item["name"]: {
                "isDir": 1 if item["is_dir"] else 0,
                "size": None if item["is_dir"] else int(item.get("size") or 0),
                "fingerprint": item.get("fingerprint"),
            }
            for item in entries
        }
        if spec and result:
            rootPath = rootPath or path
            current = normalize_path(path)
            root = normalize_path(rootPath)
            if current == root:
                relative = ""
            elif current.startswith(root.rstrip("/") + "/"):
                relative = current[len(root.rstrip("/")) + 1:] + "/"
            else:
                relative = current.lstrip("/") + "/"
            result = checkExs(relative, result, spec)
        return result

    def filePathList(self, path):
        return [{"path": item["name"]} for item in self._entries(path, 0)
                if item["is_dir"]]

    def allFileList(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        rootPath = rootPath or path
        result = self.fileListApi(path, useCache, scanInterval, spec, rootPath)
        for name in list(result.keys()):
            if name.endswith("/"):
                result[name] = self.allFileList(
                    normalize_path(path).rstrip("/") + "/" + name,
                    useCache, scanInterval, spec, rootPath,
                )
        return result

    def mkdir(self, path, scanInterval=0):
        self.checkWait(path, scanInterval)
        normalized = normalize_path(path)
        mount, relative = self.resolve(normalized)
        if relative != "/":
            self.getDriver(mount).mkdir(relative)
            self._invalidateMountCache(mount["id"])

    def deleteFile(self, path, names, scanInterval=0):
        self.checkWait(path, scanInterval)
        for name in names:
            full_path = child_path(path, name)
            mount, relative = self.resolve(full_path)
            if relative == "/":
                raise ValueError("不能删除一个挂载存储根目录")
            try:
                self.getDriver(mount).delete(relative)
            except FileNotFoundError:
                pass
            self._invalidateMountCache(mount["id"])

    def fileSize(self, mount, relative):
        parent, name = normalize_path_like_split(relative)
        for item in self.getDriver(mount).list(parent or "/", details=True):
            if item["name"] == name and not item["is_dir"]:
                return int(item.get("size") or 0)
        raise FileNotFoundError(relative)

    def copyFile(self, srcDir, dstDir, name):
        return self._startCopyTask(srcDir, dstDir, name, delete_source=False)

    def _startCopyTask(self, srcDir, dstDir, name, delete_source):
        source = child_path(srcDir, name)
        destination = child_path(dstDir, name)
        task = _CopyTask(self, source, destination, delete_source=delete_source)
        with self.tasks_lock:
            self.tasks[task.id] = task
        task.start()
        return task.id

    def moveFile(self, srcDir, dstDir, name):
        return self._startCopyTask(srcDir, dstDir, name, delete_source=True)

    def _forgetTask(self, taskId, expected=None):
        with self.tasks_lock:
            current = self.tasks.get(str(taskId))
            if expected is None or current is expected:
                self.tasks.pop(str(taskId), None)

    def _task(self, taskId):
        with self.tasks_lock:
            task = self.tasks.get(str(taskId))
        if task is None:
            raise FileNotFoundError("404 internal copy task not found")
        return task

    def taskInfo(self, taskId):
        return self._task(taskId).info()

    def copyTaskDone(self):
        with self.tasks_lock:
            tasks = list(self.tasks.values())
        return [task.info() for task in tasks if task.info()["state"] in (2, 4, 7)]

    def copyTaskUnDone(self):
        with self.tasks_lock:
            tasks = list(self.tasks.values())
        return [task.info() for task in tasks if task.info()["state"] not in (2, 4, 7)]

    def copyTaskRetry(self, taskId):
        old = self._task(taskId)
        if old.info()["state"] not in (4, 7):
            raise ValueError("只有失败或取消的任务可以重试")
        task = _CopyTask(self, old.source_path, old.destination_path, old.delete_source)
        task.id = old.id
        with self.tasks_lock:
            self.tasks[task.id] = task
        task.start()

    def copyTaskClearSucceeded(self):
        with self.tasks_lock:
            for taskId in [key for key, task in self.tasks.items()
                           if task.info()["state"] == 2]:
                del self.tasks[taskId]

    def copyTaskDelete(self, taskId):
        task = self._task(taskId)
        if task.info()["state"] not in (2, 4, 7):
            task.cancel()
            task.deleteAfterFinish()
            return
        self._forgetTask(taskId)

    def copyTaskCancel(self, taskId):
        self._task(taskId).cancel()


def normalize_path_like_split(relative):
    rel = normalize_path(relative)
    if rel == "/":
        return "/", ""
    parent, name = rel.rsplit("/", 1)
    return parent or "/", name


# =========================================================================
# 客户端缓存管理（对应 engineService）
# =========================================================================
def _get_engine_lock(engine_id):
    with _client_lock:
        lock = _engine_locks.get(engine_id)
        if lock is None:
            lock = threading.Lock()
            _engine_locks[engine_id] = lock
        return lock


def get_client_by_id(engine_id, session_factory):
    """返回某引擎的客户端（内置 taosync 用 TaoSyncClient，外部 AList 用 AlistClient）。"""
    engine_id = int(engine_id)
    with _client_lock:
        client = _client_list.get(engine_id)
        if client is not None:
            return client
    with _get_engine_lock(engine_id):
        with _client_lock:
            client = _client_list.get(engine_id)
            if client is not None:
                return client
        engine_dao = SyncEngineDAO(session_factory)
        engine = engine_dao.get_engine_by_id(engine_id)
        if engine.get("engineType") == "taosync":
            mount_dao = StorageMountDAO(session_factory)
            storage_service = StorageService(session_factory)
            client = TaoSyncClient(engine_id, mount_dao, storage_service)
        else:
            client = AlistClient(engine["url"], engine["token"], engine_id)
        with _client_lock:
            _client_list[engine_id] = client
        return client


def invalidate_client(engine_id):
    engine_id = int(engine_id)
    with _get_engine_lock(engine_id):
        with _client_lock:
            _client_list.pop(engine_id, None)


def _invalidate_engine(engine_id):
    """清空某引擎的源快照并使其客户端缓存失效。"""
    from core.sync.job_dao import clear_source_snapshots_by_engine
    try:
        clear_source_snapshots_by_engine(int(engine_id))
    except Exception:
        pass
    invalidate_client(engine_id)
