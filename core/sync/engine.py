"""存储目录（挂载）DAO + TaoSyncClient facade + 引擎管理。

移植自 TaoSync 的 ``storageService.py`` / ``engineService.py`` / ``taosyncClient.py``，
将 DAO 改为 SQLAlchemy 会话操作（``models_sync`` 的 ORM 模型），逻辑保持一致。

内置引擎固定为 ``taosync`` 系统引擎（``systemKey='taosync'``），挂载对应五种 driver：
local / smb / ftp / sftp / aliyun。``TaoSyncClient`` 作为 AList 兼容 facade，
把虚拟路径 ``/挂载名/相对路径`` 解析到对应 driver，供同步任务引擎调用。
"""
from __future__ import annotations

import posixpath
import re
import threading
import time

from core.sync_storage.base import (
    TransferCancelled,
    check_cancel,
    child_path,
    normalize_path,
    stream_transfer,
)
from core.sync_storage.factory import SECRET_FIELDS, createDriver, getDriverTypes
from core.sync_storage.pathIdentity import mount_paths_overlap, virtual_paths_overlap
from models_sync import SyncEngine, SyncStorageMount

MOUNT_NAME_PATTERN = re.compile(r"^[^\\/:\x00-\x1f]+$")

# 内置 taosync 系统引擎的 remark / systemKey（与 TaoSync 一致）。
SYSTEM_ENGINE_REMARK = "TaoSync"
SYSTEM_ENGINE_KEY = "taosync"


def ensure_system_engine(db) -> SyncEngine:
    """确保内置 taosync 系统引擎存在并返回（幂等）。"""
    engine = (
        db.query(SyncEngine)
        .filter_by(systemKey=SYSTEM_ENGINE_KEY, engineType="taosync")
        .first()
    )
    if engine is None:
        engine = SyncEngine(
            remark=SYSTEM_ENGINE_REMARK,
            url="",
            userName="",
            token="",
            engineType="taosync",
            systemKey=SYSTEM_ENGINE_KEY,
            protected=1,
            createTime=0,
        )
        db.add(engine)
        db.flush()
    return engine


def get_system_engine_id(db) -> int:
    return ensure_system_engine(db).id


def _clean_name(name):
    name = str(name or "").strip()
    if not name or name in (".", "..") or not MOUNT_NAME_PATTERN.match(name):
        raise ValueError("invalid storage directory name")
    return name


def _sanitized(row: SyncStorageMount) -> dict:
    """序列化挂载为对外 dict，遮蔽 secret 字段并用 secretState 标记是否已配置。"""
    config = dict(row.config_dict())
    secret_state = {}
    for field in SECRET_FIELDS.get(row.driverType, set()):
        secret_state[field] = bool(config.get(field))
        config[field] = ""
    return {
        "id": row.id,
        "engineId": row.engineId,
        "name": row.name,
        "driverType": row.driverType,
        "config": config,
        "enabled": row.enabled,
        "authVersion": row.authVersion,
        "configVersion": row.configVersion,
        "createTime": row.createTime,
        "secretState": secret_state,
    }


def get_mount_list(db, engine_id: int) -> list:
    rows = (
        db.query(SyncStorageMount)
        .filter_by(engineId=engine_id, enabled=1)
        .order_by(SyncStorageMount.id.asc())
        .all()
    )
    return [_sanitized(row) for row in rows]


def get_mount_by_id(db, mount_id: int) -> SyncStorageMount:
    row = db.query(SyncStorageMount).filter_by(id=mount_id).first()
    if row is None:
        raise ValueError("storage mount not found")
    return row


def get_supported_drivers() -> list:
    return getDriverTypes()


def engine_mounts_overlap(db, engine_id: int, first_path: str, second_path: str) -> bool:
    mounts = get_mount_list(db, engine_id)
    return mount_paths_overlap(mounts, first_path, second_path)


def _validate_unique_name(db, engine_id: int, name: str, exclude_id=None):
    for row in db.query(SyncStorageMount).filter_by(engineId=engine_id).all():
        if row.name.casefold() == name.casefold() and row.id != exclude_id:
            raise ValueError("a storage directory with the same name already exists")


def _normalize_driver_config(driver_type: str, config: dict) -> dict:
    driver = createDriver(driver_type, config)
    # 网络凭证在首次浏览时校验；不要在此轮换阿里云 refresh token。
    if driver_type == "local":
        driver.list("/")
    return dict(getattr(driver, "config", config))


def _sftp_auth_type(config):
    explicit = str(config.get("auth_type") or "").strip().lower()
    if explicit:
        return explicit
    return "private_key" if str(config.get("private_key") or "").strip() else "password"


def _sftp_connection_identity(config):
    try:
        port = int(config.get("port") or 22)
    except (TypeError, ValueError):
        port = config.get("port")
    return (
        str(config.get("host") or "").strip(),
        port,
        str(config.get("username") or "").strip(),
        _sftp_auth_type(config),
    )


def _prepare_sftp_config(raw_config: dict, old=None):
    old_config = dict((old or {}).config_dict() if old is not None else {})
    config = {**old_config, **dict(raw_config)}
    same_identity = old is not None and _sftp_connection_identity(
        config
    ) == _sftp_connection_identity(old_config)
    auth_type = _sftp_auth_type(config)
    config["auth_type"] = auth_type
    if auth_type == "password":
        password = str(raw_config.get("password") or "")
        if not password and same_identity:
            password = str(old_config.get("password") or "")
        config["password"] = password
        config["private_key"] = ""
        config["private_key_passphrase"] = ""
        if not config["password"]:
            raise ValueError("SFTP password is required for password authentication")
    elif auth_type == "private_key":
        private_key = str(raw_config.get("private_key") or "")
        if private_key:
            passphrase = str(raw_config.get("private_key_passphrase") or "")
        elif same_identity:
            private_key = str(old_config.get("private_key") or "")
            passphrase = str(
                raw_config.get("private_key_passphrase")
                or old_config.get("private_key_passphrase")
                or ""
            )
        else:
            passphrase = ""
        config["private_key"] = private_key
        config["private_key_passphrase"] = passphrase
        config["password"] = ""
        if not config["private_key"].strip():
            raise ValueError(
                "SFTP private_key is required for private key authentication"
            )
    else:
        config["password"] = ""
        config["private_key"] = ""
        config["private_key_passphrase"] = ""
    return config


def _sftp_request_config(db, data: dict):
    engine_id = int(data["engineId"])
    raw_config = data.get("config")
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("storage configuration must be an object")

    old = None
    mount_id = data.get("mountId")
    if mount_id not in (None, ""):
        old = get_mount_by_id(db, int(mount_id))
        if int(old.engineId) != engine_id:
            raise ValueError("storage directory does not belong to the selected engine")
        if old.driverType != "sftp":
            raise ValueError("storage directory is not an SFTP mount")
    return _prepare_sftp_config(raw_config, old=old)


def test_sftp(db, data: dict) -> dict:
    """探测 SFTP 凭证而不新增/更新挂载。"""
    return createDriver("sftp", _sftp_request_config(db, data)).probe()


def browse_sftp(db, data: dict) -> dict:
    """列出一级 SFTP 目录而不持久化配置。"""
    if "path" not in data:
        raise ValueError("SFTP browse path is required")
    return createDriver("sftp", _sftp_request_config(db, data)).browse(data["path"])


def _smb_request_config(db, data: dict):
    engine_id = int(data["engineId"])
    raw_config = data.get("config")
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("storage configuration must be an object")

    old = None
    mount_id = data.get("mountId")
    if mount_id not in (None, ""):
        old = get_mount_by_id(db, int(mount_id))
        if int(old.engineId) != engine_id:
            raise ValueError("storage directory does not belong to the selected engine")
        if old.driverType != "smb":
            raise ValueError("storage directory is not an SMB mount")

    config = {**dict((old or {}).config_dict() if old is not None else {}), **dict(raw_config)}
    # 密码在编辑时由前端遮蔽，空缺则沿用已存储值。
    if not config.get("password") and old is not None:
        config["password"] = old.config_dict().get("password", "")
    return config


def discover_smb_shares(db, data: dict) -> list:
    """列出 SMB 主机共享而不创建/更新挂载。"""
    from core.sync_storage.discoveryService import list_smb_shares

    return list_smb_shares(_smb_request_config(db, data))


def add_mount(db, data: dict) -> int:
    engine_id = int(data["engineId"])
    name = _clean_name(data.get("name"))
    driver_type = str(data.get("driverType") or "").strip().lower()
    raw_config = data.get("config") or {}
    if not isinstance(raw_config, dict):
        raise ValueError("storage configuration must be an object")
    config = (
        _prepare_sftp_config(raw_config)
        if driver_type == "sftp"
        else dict(raw_config)
    )
    _validate_unique_name(db, engine_id, name)
    config = _normalize_driver_config(driver_type, config)
    mount = SyncStorageMount(
        engineId=engine_id,
        name=name,
        driverType=driver_type,
        config=__import__("json").dumps(config, ensure_ascii=False),
        enabled=1,
    )
    db.add(mount)
    db.flush()
    return mount.id


def update_mount(db, data: dict) -> None:
    mount_id = int(data["id"])
    old = get_mount_by_id(db, mount_id)
    name = _clean_name(data.get("name", old.name))
    if name != old.name:
        raise ValueError("storage directory names cannot be changed; remove and recreate the directory")
    driver_type = str(data.get("driverType") or old.driverType).strip().lower()
    if driver_type != old.driverType:
        raise ValueError("storage driver type cannot be changed")
    raw_config = data.get("config") or {}
    if not isinstance(raw_config, dict):
        raise ValueError("storage configuration must be an object")
    old_config = old.config_dict()
    if driver_type == "sftp":
        config = _prepare_sftp_config(raw_config, old=old)
    else:
        config = {**old_config, **dict(raw_config)}
        for secret in SECRET_FIELDS.get(driver_type, set()):
            if not config.get(secret):
                config[secret] = old_config.get(secret, "")
    auth_fields = {"client_id", "client_secret", "refresh_token", "api_url", "oauth_url"}
    auth_changed = driver_type == "aliyun" and any(
        old_config.get(key) != config.get(key) for key in auth_fields
    )
    drive_changed = (
        driver_type == "aliyun"
        and old_config.get("drive_type", "resource") != config.get("drive_type", "resource")
    )
    if auth_changed:
        for key in ("access_token", "expires_at", "drive_id"):
            config.pop(key, None)
    elif drive_changed:
        config.pop("drive_id", None)
    config = _normalize_driver_config(driver_type, config)
    old.config = __import__("json").dumps(config, ensure_ascii=False)
    old.name = name
    old.enabled = int(data.get("enabled", old.enabled))
    if auth_changed or drive_changed:
        old.authVersion = (old.authVersion or 1) + 1
    db.flush()
    invalidate_client(old.engineId)


def update_mount_config(db, mount_id: int, expected_auth_version: int, config: dict,
                        expected_tokens=None):
    """持久化轮换后的云端 token，不覆盖其它已编辑字段。"""
    row = get_mount_by_id(db, int(mount_id))
    if int(row.authVersion or 1) != int(expected_auth_version):
        return None
    current = row.config_dict()
    token_fields = {"access_token", "refresh_token", "expires_at", "drive_id"}
    if expected_tokens and any(
        current.get(key) != value for key, value in expected_tokens.items()
    ):
        return {"conflict": True, "config": current}
    for key in token_fields:
        if key in config:
            current[key] = config[key]
    row.config = __import__("json").dumps(current, ensure_ascii=False)
    row.authVersion = int(expected_auth_version)
    db.flush()
    invalidate_client(row.engineId)
    return True


def remove_mount(db, mount_id: int) -> None:
    row = get_mount_by_id(db, int(mount_id))
    db.delete(row)
    invalidate_client(row.engineId)


# ============================================================
# TaoSyncClient —— AList 兼容 facade
# ============================================================

_client_cache: dict = {}
_client_lock = threading.Lock()
_engine_locks: dict = {}


def _get_engine_lock(engine_id: int):
    with _client_lock:
        lock = _engine_locks.get(engine_id)
        if lock is None:
            lock = threading.Lock()
            _engine_locks[engine_id] = lock
        return lock


def invalidate_client(engine_id: int) -> None:
    engine_id = int(engine_id)
    with _get_engine_lock(engine_id):
        with _client_lock:
            _client_cache.pop(engine_id, None)


def get_storage_client(db, engine_id: int) -> "TaoSyncClient":
    engine_id = int(engine_id)
    with _client_lock:
        client = _client_cache.get(engine_id)
        if client is not None:
            return client
    with _get_engine_lock(engine_id):
        with _client_lock:
            client = _client_cache.get(engine_id)
            if client is not None:
                return client
        client = TaoSyncClient(db, engine_id)
        with _client_lock:
            _client_cache[engine_id] = client
        return client


class _CopyTask:
    def __init__(self, client, source_path, destination_path, delete_source=False):
        self.client = client
        self.id = __import__("uuid").uuid4().hex
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
            target=self._run, name="taosync-copy-" + self.id[:8], daemon=True
        )
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
            destination_mount, destination_relative = self.client.resolve(self.destination_path)
            size = self.client.file_size(source_mount, source_relative)
            source_driver = self.client.get_driver(source_mount)
            if source_mount["id"] == destination_mount["id"]:
                source_driver.copy(
                    source_relative,
                    destination_relative,
                    size=size,
                    progress=self._report,
                    cancel=self.cancel_event,
                )
            else:
                destination_driver = self.client.get_driver(destination_mount)
                stream_transfer(
                    source_driver,
                    source_relative,
                    destination_driver,
                    destination_relative,
                    size,
                    self._report,
                    self.cancel_event,
                )
            self.client._invalidate_mount_cache(destination_mount["id"])
            if self.delete_source:
                # 目标已提交完成。取消信号到达后不得删除源（避免破坏已移动数据）。
                if self.cancel_event.is_set():
                    with self.lock:
                        self.progress = 100.0
                        self.state = 2
                    return
                source_driver.delete(source_relative)
                self.client._invalidate_mount_cache(source_mount["id"])
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
                self.client._forget_task(self.id, self)

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

    def delete_after_finish(self):
        with self.lock:
            self.delete_when_done = True
            terminal = self.state in (2, 4, 7)
        if terminal:
            self.client._forget_task(self.id, self)


class TaoSyncClient:
    """AList 风格的 TaoSync 内部多后端 driver facade。"""

    def __init__(self, db, engine_id: int):
        self.db = db
        self.alist_id = int(engine_id)
        self.user = SYSTEM_ENGINE_REMARK
        self.waits = {}
        self.tasks = {}
        self.tasks_lock = threading.Lock()
        self.mounts = {}
        self.entry_cache = {}
        self.entry_cache_lock = threading.Lock()
        self.cache_ttl = 15.0
        for row in db.query(SyncStorageMount).filter_by(
            engineId=self.alist_id, enabled=1
        ).all():
            mount_id = int(row.id)
            auth_version = int(row.authVersion or 1)
            config = row.config_dict()

            def save_config(cfg, expected_tokens=None, current_id=mount_id,
                            current_auth_version=auth_version):
                return update_mount_config(
                    self.db, current_id, current_auth_version, cfg,
                    expected_tokens=expected_tokens,
                )

            def load_config(current_id=mount_id):
                return get_mount_by_id(self.db, current_id).config_dict()

            mount = {
                "id": mount_id,
                "engineId": row.engineId,
                "name": row.name,
                "driverType": row.driverType,
                "config": config,
                "enabled": row.enabled,
                "authVersion": auth_version,
            }
            try:
                mount["driver"] = createDriver(
                    row.driverType,
                    config,
                    save_config=save_config,
                    load_config=load_config,
                    refresh_lock=_get_mount_refresh_lock(mount_id, auth_version),
                    auth_version=auth_version,
                )
                mount["driverError"] = ""
            except Exception as exc:
                mount["driver"] = None
                mount["driverError"] = str(exc)
            self.mounts[mount["name"]] = mount

    def mount_paths_overlap(self, first_path, second_path):
        return mount_paths_overlap(self.mounts, first_path, second_path)

    def paths_overlap(self, first_path, second_path):
        return (
            virtual_paths_overlap(first_path, second_path, case_sensitive=True)
            or self.mount_paths_overlap(first_path, second_path)
        )

    def check_wait(self, path, scan_interval=0):
        if not scan_interval:
            return
        normalized = normalize_path(path)
        root = normalized.strip("/").split("/", 1)[0] if normalized != "/" else "/"
        if root in self.waits:
            elapsed = time.monotonic() - self.waits[root]
            if elapsed < scan_interval:
                time.sleep(scan_interval - elapsed)
        self.waits[root] = time.monotonic()

    def resolve(self, path):
        normalized = normalize_path(path, allow_root=False)
        parts = normalized.strip("/").split("/")
        mount = self.mounts.get(parts[0])
        if mount is None:
            raise FileNotFoundError("storage directory not found: " + parts[0])
        relative = "/" + "/".join(parts[1:]) if len(parts) > 1 else "/"
        return mount, relative

    @staticmethod
    def get_driver(mount):
        driver = mount.get("driver")
        if driver is None:
            detail = mount.get("driverError") or "driver could not be initialized"
            raise RuntimeError(
                "storage directory '{}' is unavailable: {}".format(
                    mount.get("name", ""), detail
                )
            )
        return driver

    def _invalidate_mount_cache(self, mount_id=None):
        with self.entry_cache_lock:
            if mount_id is None:
                self.entry_cache.clear()
            else:
                for key in [key for key in self.entry_cache if key[0] == mount_id]:
                    self.entry_cache.pop(key, None)

    def _entries(self, path, use_cache=0):
        normalized = normalize_path(path)
        if normalized == "/":
            return [{"name": name, "is_dir": True, "size": None} for name in sorted(self.mounts)]
        mount, relative = self.resolve(normalized)
        cache_key = (mount["id"], relative)
        if int(use_cache or 0) == 1:
            with self.entry_cache_lock:
                cached = self.entry_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.cache_ttl:
                return [dict(item) for item in cached[1]]
        entries = self.get_driver(mount).list(relative, details=True)
        with self.entry_cache_lock:
            self.entry_cache[cache_key] = (time.monotonic(), [dict(item) for item in entries])
        return entries

    def file_list_detail_api(self, path, use_cache=0, scan_interval=0, spec=None, root_path=None):
        self.check_wait(path, scan_interval)
        entries = self._entries(path, use_cache)
        result = {
            item["name"] + "/" if item["is_dir"] else item["name"]: {
                "isDir": 1 if item["is_dir"] else 0,
                "size": None if item["is_dir"] else int(item.get("size") or 0),
                "fingerprint": item.get("fingerprint"),
            }
            for item in entries
        }
        if spec and result:
            from core.sync_storage.alist_compat import check_exs
            root_path = root_path or path
            current = normalize_path(path)
            root = normalize_path(root_path)
            if current == root:
                relative = ""
            elif current.startswith(root.rstrip("/") + "/"):
                relative = current[len(root.rstrip("/")) + 1:] + "/"
            else:
                relative = current.lstrip("/") + "/"
            result = check_exs(relative, result, spec)
        return result

    def file_list_api(self, path, use_cache=0, scan_interval=0, spec=None, root_path=None):
        details = self.file_list_detail_api(path, use_cache, scan_interval, spec, root_path)
        return {
            name: {} if detail["isDir"] else detail["size"]
            for name, detail in details.items()
        }

    def file_path_list(self, path):
        return [{"path": item["name"]} for item in self._entries(path, 0) if item["is_dir"]]

    def all_file_list(self, path, use_cache=0, scan_interval=0, spec=None, root_path=None):
        root_path = root_path or path
        result = self.file_list_api(path, use_cache, scan_interval, spec, root_path)
        for name in list(result.keys()):
            if name.endswith("/"):
                result[name] = self.all_file_list(
                    normalize_path(path).rstrip("/") + "/" + name,
                    use_cache, scan_interval, spec, root_path,
                )
        return result

    def mkdir(self, path, scan_interval=0):
        self.check_wait(path, scan_interval)
        normalized = normalize_path(path)
        mount, relative = self.resolve(normalized)
        if relative != "/":
            self.get_driver(mount).mkdir(relative)
            self._invalidate_mount_cache(mount["id"])

    def delete_file(self, path, names, scan_interval=0):
        self.check_wait(path, scan_interval)
        for name in names:
            full_path = child_path(path, name)
            mount, relative = self.resolve(full_path)
            if relative == "/":
                raise ValueError("a mounted storage root cannot be deleted by a job")
            try:
                self.get_driver(mount).delete(relative)
            except FileNotFoundError:
                pass
            self._invalidate_mount_cache(mount["id"])

    def file_size(self, mount, relative):
        parent, name = posixpath.split(relative)
        for item in self.get_driver(mount).list(parent or "/"):
            if item["name"] == name and not item["is_dir"]:
                return int(item.get("size") or 0)
        raise FileNotFoundError(relative)

    def copy_file(self, src_dir, dst_dir, name):
        return self._start_copy_task(src_dir, dst_dir, name, delete_source=False)

    def _start_copy_task(self, src_dir, dst_dir, name, delete_source):
        source = child_path(src_dir, name)
        destination = child_path(dst_dir, name)
        task = _CopyTask(self, source, destination, delete_source=delete_source)
        with self.tasks_lock:
            self.tasks[task.id] = task
        task.start()
        return task.id

    def move_file(self, src_dir, dst_dir, name):
        return self._start_copy_task(src_dir, dst_dir, name, delete_source=True)

    def _forget_task(self, task_id, expected=None):
        with self.tasks_lock:
            current = self.tasks.get(str(task_id))
            if expected is None or current is expected:
                self.tasks.pop(str(task_id), None)

    def _task(self, task_id):
        with self.tasks_lock:
            task = self.tasks.get(str(task_id))
        if task is None:
            raise FileNotFoundError("404 internal copy task not found")
        return task

    def task_info(self, task_id):
        return self._task(task_id).info()

    def copy_task_done(self):
        with self.tasks_lock:
            tasks = list(self.tasks.values())
        return [task.info() for task in tasks if task.info()["state"] in (2, 4, 7)]

    def copy_task_un_done(self):
        with self.tasks_lock:
            tasks = list(self.tasks.values())
        return [task.info() for task in tasks if task.info()["state"] not in (2, 4, 7)]

    def copy_task_retry(self, task_id):
        old = self._task(task_id)
        if old.info()["state"] not in (4, 7):
            raise ValueError("only failed or cancelled tasks can be retried")
        task = _CopyTask(self, old.source_path, old.destination_path, old.delete_source)
        task.id = old.id
        with self.tasks_lock:
            self.tasks[task.id] = task
        task.start()

    def copy_task_clear_succeeded(self):
        with self.tasks_lock:
            for task_id in [key for key, task in self.tasks.items() if task.info()["state"] == 2]:
                del self.tasks[task_id]

    def copy_task_delete(self, task_id):
        task = self._task(task_id)
        if task.info()["state"] not in (2, 4, 7):
            task.cancel()
            task.delete_after_finish()
            return
        self._forget_task(task_id)

    def copy_task_cancel(self, task_id):
        self._task(task_id).cancel()


_mount_refresh_locks = {}
_mount_refresh_locks_guard = threading.Lock()


def _get_mount_refresh_lock(mount_id, auth_version):
    key = (int(mount_id), int(auth_version))
    with _mount_refresh_locks_guard:
        return _mount_refresh_locks.setdefault(key, threading.Lock())
