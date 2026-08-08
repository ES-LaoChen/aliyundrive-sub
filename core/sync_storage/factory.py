"""存储驱动工厂（移植自 TaoSync service/storage/factory.py）。"""
from .drivers.aliyun import AliyunDriveDriver
from .drivers.ftp import FtpDriver
from .drivers.local import LocalDriver
from .drivers.sftp import SftpDriver
from .drivers.smb import SmbDriver


DRIVER_TYPES = {
    "local": LocalDriver,
    "smb": SmbDriver,
    "ftp": FtpDriver,
    "sftp": SftpDriver,
    "aliyun": AliyunDriveDriver,
}

SECRET_FIELDS = {
    "local": set(),
    "smb": {"password"},
    "ftp": {"password"},
    "sftp": {"password", "private_key", "private_key_passphrase"},
    "aliyun": {"client_secret", "refresh_token", "access_token"},
}


def createDriver(
    driverType,
    config,
    save_config=None,
    load_config=None,
    refresh_lock=None,
    auth_version=None,
):
    driver_class = DRIVER_TYPES.get(driverType)
    if driver_class is None:
        raise ValueError("unsupported storage driver: {}".format(driverType))
    if driverType == "aliyun":
        return driver_class(
            config,
            save_config=save_config,
            load_config=load_config,
            refresh_lock=refresh_lock,
            auth_version=auth_version,
        )
    return driver_class(config)


def getDriverTypes():
    return list(DRIVER_TYPES.keys())
