"""AList / OpenList 兼容客户端与公共工具（移植自 TaoSync alistClient.py / fileFingerprint.py）。

说明：
- ``common.LNG.G`` 在源项目是 i18n 文案函数；本项目无该体系，统一改为抛出带中文
  文案的 ``Exception``，保持可读性与失败可诊断性。
- ``AlistClient`` 仅依赖 ``requests``，用于对接外部 OpenList/AList 的 ``/api/fs/*``。
"""
from __future__ import annotations

import hashlib
import json
import time

import requests

from core.sync_storage.base import normalize_path


def fileFingerprint(namespace, *components):
    """Build an opaque, deterministic version marker from backend metadata."""
    if not any(component not in (None, "", {}, []) for component in components):
        return None
    payload = json.dumps(
        [str(namespace), *components],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "{}:sha256:{}".format(
        namespace, hashlib.sha256(payload).hexdigest()
    )


def checkExs(path, rts, spec):
    """按排除规则过滤内容列表。

    :param path: 所在路径
    :param rts: 内容列表，例如
        {"test1-1/": {...}, "test1.txt": {...}}
    :param spec: 排除规则（pathspec.PathSpec）
    :return: 排除后的内容列表
    """
    rtsNew = dict(rts)
    base_path = str(path or "").strip("/")
    for rtsItem in list(rts.keys()):
        candidate = "/".join(part for part in (base_path, rtsItem) if part)
        if spec.match_file(candidate):
            del rtsNew[rtsItem]
    return rtsNew


class AlistClient:
    """外部 OpenList/AList 实例的 HTTP 客户端。"""

    def __init__(self, url, token, alistId=None):
        self.url = url
        self.user = None
        self.alistId = alistId
        self.token = token
        self.waits = {}
        self.getUser()

    def req(self, method, url, data=None, params=None):
        res = {
            'code': 500,
            'message': None,
            'data': None,
        }
        headers = None
        if self.token is not None:
            headers = {'Authorization': self.token}
        try:
            r = requests.request(
                method, self.url + url, json=data, params=params,
                headers=headers, timeout=(60, 300),
            )
            if r.status_code == 200:
                res = r.json()
            else:
                res['code'] = r.status_code
                res['message'] = "HTTP 非 200"
        except Exception as e:
            if 'Invalid URL' in str(e):
                raise Exception("AList 地址格式不正确")
            elif 'Max retries' in str(e):
                raise Exception("无法连接 AList 服务")
            raise Exception(str(e))
        if res['code'] != 200:
            if res['code'] == 401:
                raise Exception("AList 鉴权失败（token 无效）")
            raise Exception("AList 请求失败：{} {}".format(res['code'], res['message']))
        return res['data']

    def post(self, url, data=None, params=None):
        return self.req('post', url, data, params)

    def get(self, url, params=None):
        return self.req('get', url, params=params)

    def getUser(self):
        self.user = self.get('/api/me')['username']

    def updateAlistId(self, alistId):
        self.alistId = alistId

    def checkWait(self, path, scanInterval=0):
        if scanInterval != 0:
            pathFirst = path.split('/', maxsplit=2)[1]
            if pathFirst in self.waits:
                timeC = time.time() - self.waits[pathFirst]
                if timeC < scanInterval:
                    self.waits[pathFirst] = time.time() + timeC
                    time.sleep(scanInterval - timeC)
                    return
            self.waits[pathFirst] = time.time()

    def fileListApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        details = self.fileListDetailApi(path, useCache, scanInterval, spec, rootPath)
        return {
            name: {} if detail['isDir'] else detail['size']
            for name, detail in details.items()
        }

    def fileListDetailApi(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        """Return AList entries with stable metadata while keeping fileListApi compatible."""
        self.checkWait(path, scanInterval)
        res = self.post('/api/fs/list', data={
            'path': path,
            'refresh': useCache != 1,
        })['content']
        if res is not None:
            rts = {
                f"{item['name']}/" if item['is_dir'] else item['name']: {
                    'isDir': 1 if item['is_dir'] else 0,
                    'size': None if item['is_dir'] else item['size'],
                    'fingerprint': fileFingerprint(
                        'alist',
                        item.get('hash_info') or item.get('hash'),
                        item.get('modified') or item.get('updated_at'),
                    ),
                } for item in res
            }
        else:
            rts = {}
        if spec and rts:
            if rootPath is None:
                rootPath = path
            rts = checkExs(path[len(rootPath):], rts, spec)
        return rts

    def filePathList(self, path):
        res = self.post('/api/fs/list', data={
            'path': path,
            'refresh': True,
        })['content']
        if res is not None:
            return [{'path': item['name']} for item in res if item['is_dir']]
        return []

    def mkdir(self, path, scanInterval=0):
        self.checkWait(path, scanInterval)
        return self.post('/api/fs/mkdir', data={'path': path})

    def deleteFile(self, path, names, scanInterval=0):
        self.checkWait(path, scanInterval)
        self.post('/api/fs/remove', data={'names': names, 'dir': path})

    def copyFile(self, srcDir, dstDir, name):
        tasks = self.post('/api/fs/copy', data={
            'src_dir': srcDir,
            'dst_dir': dstDir,
            'overwrite': True,
            'names': [name],
        })['tasks']
        if tasks:
            return tasks[0]['id']
        return None

    def moveFile(self, srcDir, dstDir, name):
        tasks = self.post('/api/fs/move', data={
            'src_dir': srcDir,
            'dst_dir': dstDir,
            'overwrite': True,
            'names': [name],
        })['tasks']
        if tasks:
            return tasks[0]['id']
        return None

    def taskInfo(self, taskId):
        return self.post('/api/admin/task/copy/info', params={'tid': taskId})

    def copyTaskDone(self):
        return self.get('/api/admin/task/copy/done')

    def copyTaskUnDone(self):
        return self.get('/api/admin/task/copy/undone')

    def copyTaskRetry(self, taskId):
        self.post('/api/admin/task/copy/retry', params={'tid': taskId})

    def copyTaskClearSucceeded(self):
        self.post('/api/admin/task/copy/clear_succeeded')

    def copyTaskDelete(self, taskId):
        self.post('/api/admin/task/copy/delete', params={'tid': taskId})

    def copyTaskCancel(self, taskId):
        self.post('/api/admin/task/copy/cancel', params={'tid': taskId})
