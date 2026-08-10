"""同步作业客户端（移植自 TaoSync service/syncJob/jobClient.py）。

保持原核心逻辑：调度（interval/cron/manual）、增量 source-mode 快照同步、
move 模式、冲突处理、排除规则（pathspec）、文件大小过滤、单文件进度、并行提交。

适配差异：
- ``engineService.getClientById`` → ``core.sync_storage.engine.get_client_by_id``。
- 文案 ``common.LNG.G(...)`` → 中文字面量。
- 通知经 ``SyncService`` 传入的 ``notifier``；``taskService.updateJobTaskStatus``
  已封装通知调用。
"""
from __future__ import annotations

import itertools
import logging
import posixpath
import threading
import time
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern

from core.sync import job_dao
from core.sync.job_client_helpers import is_file_size_allowed, virtual_paths_overlap
from core.sync.move_log import append_moved_file, load_moved_file_names
from core.sync_storage.engine import get_client_by_id

logger = logging.getLogger(__name__)


class CopyItem:
    def __init__(self, srcPath, dstPath, fileName, fileSize, method, jobTask):
        self.jobTask = jobTask
        self.alistClient = self.jobTask.alistClient
        self.taskId = self.jobTask.taskId
        self.srcPath = srcPath
        self.dstPath = dstPath
        self.fileName = fileName
        self.fileSize = fileSize
        self.copyType = 0 if method < 2 else 2
        self.alistTaskId = None
        self.status = 0
        self.progress = 0.0
        self.errMsg = None
        self.createTime = int(time.time())
        self.doingKey = None

    def doByThread(self):
        doThread = threading.Thread(target=self.doIt)
        doThread.start()

    def doIt(self):
        try:
            if self.jobTask.breakFlag:
                self.status = 4
            else:
                self.alistTaskId = self.alistClient.copyFile(
                    self.srcPath, self.dstPath, self.fileName)
        except Exception as e:
            self.errMsg = str(e)
            self.status = 7
        else:
            if self.alistTaskId is None:
                self.status = 2
            elif self.status != 4:
                self.checkAndGetStatus()
        self.endIt()

    def checkAndGetStatus(self):
        while True:
            if self.jobTask.breakFlag:
                self.status = 4
                if self.alistTaskId is not None:
                    try:
                        self.alistClient.copyTaskCancel(self.alistTaskId)
                        self.alistClient.copyTaskDelete(self.alistTaskId)
                    except Exception:
                        self.status = 7
                break
            cuTime = time.time()
            time.sleep(0.61 if cuTime - self.jobTask.lastWatching < 3 else 2.93)
            try:
                taskInfo = self.alistClient.taskInfo(self.alistTaskId)
            except Exception as e:
                logger.exception(e)
                eMsg = str(e)
                if '404' in eMsg:
                    eMsg = "任务可能已被删除"
                taskInfo = {'state': 7, 'progress': None, 'error': eMsg}
            if taskInfo['state'] == self.status and taskInfo['progress'] == self.progress:
                continue
            self.status = taskInfo['state']
            self.progress = taskInfo['progress']
            self.errMsg = taskInfo['error'] if taskInfo['error'] else None
            if taskInfo['state'] in [2, 4, 7]:
                try:
                    self.alistClient.copyTaskDelete(self.alistTaskId)
                    break
                except Exception:
                    break

    def endIt(self):
        self.jobTask.copyHook(
            self.srcPath, self.dstPath, self.fileName, self.fileSize,
            self.alistTaskId, self.status, errMsg=self.errMsg, copyType=self.copyType,
            createTime=self.createTime,
        )
        del self.jobTask.doing[self.doingKey]


class JobTask:
    def __init__(self, taskId, vm, notifier=None, session_factory=None):
        self.taskId = taskId
        self.jobClient = vm
        self.job = self.jobClient.job
        self.alistClient = get_client_by_id(self.job['alistId'], session_factory)
        self._notifier = notifier
        self._session_factory = session_factory
        self.createTime = time.time()
        self.finish = []
        self.doing = {}
        self.waiting = []
        self.lastWatching = 0.0
        self.queueNum = 0
        self.scanFinish = False
        self.firstSync = None
        self.breakFlag = False
        self.sourceSnapshot = {}
        self.sourceScanAttempted = False
        self.sourceScanFailed = False
        self.previousSourceSnapshot = None
        self.sourceSnapshotIdentity = job_dao.source_snapshot_identity(self.job)
        self.currentTasks = {}
        self.movedFileNames = (
            load_moved_file_names(self.job['id'], session_factory)
            if self.job.get('method') == 2 else set()
        )
        self.syncThread = threading.Thread(target=self.sync)
        self.submitThread = threading.Thread(target=self.taskSubmit)

    def start(self):
        self.syncThread.start()
        self.submitThread.start()

    def getCurrent(self):
        self.lastWatching = time.time()
        waits = [{
            'srcPath': w.srcPath, 'dstPath': w.dstPath, 'isPath': 0,
            'fileName': w.fileName, 'fileSize': w.fileSize, 'status': w.status,
            'type': w.copyType, 'progress': w.progress, 'errMsg': w.errMsg,
            'createTime': w.createTime,
        } for w in self.waiting]
        dos = [{
            'srcPath': d.srcPath, 'dstPath': d.dstPath, 'isPath': 0,
            'fileName': d.fileName, 'fileSize': d.fileSize, 'status': d.status,
            'type': d.copyType, 'progress': d.progress, 'errMsg': d.errMsg,
            'createTime': d.createTime,
        } for d in self.doing.values()]
        allTask = list(itertools.chain(waits, dos, self.finish))
        keyValSpace = {
            'wait': 0, 'running': 1, 'success': 2, 'fail': 7, 'other': -1,
        }
        currentTasks = {}
        for val in keyValSpace.values():
            currentTasks[val] = []
        otk = []
        otkStatus = [3, 4, 5, 6, 8, 9]
        grouped = defaultdict(list)
        for taskItem in allTask:
            grouped[taskItem['status']].append(taskItem)
        for status, tasks in grouped.items():
            tasks.sort(key=lambda x: x['createTime'])
            if status in otkStatus:
                otk.extend(tasks)
            else:
                currentTasks[status] = tasks
        currentTasks[-1] = otk
        self.currentTasks = currentTasks
        result = {
            'scanFinish': self.scanFinish,
            'doingTask': currentTasks[1],
            'createTime': int(self.createTime),
            'duration': int(self.lastWatching - self.createTime),
            'firstSync': int(self.firstSync) if self.firstSync is not None else None,
            'num': {}, 'size': {},
        }
        for key, val in keyValSpace.items():
            result['num'][key] = len(currentTasks[val])
            result['size'][key] = sum(
                item['fileSize'] for item in currentTasks[val]
                if item['fileSize'] is not None and item['type'] != 1)
        return result

    def getCurrentByStatus(self, status):
        return self.currentTasks[status]

    def taskSubmit(self):
        while True:
            if self.breakFlag:
                break
            time.sleep(0.5)
            doingNums = len(self.doing.keys())
            waitingNums = len(self.waiting)
            if not self.scanFinish or doingNums != 0 or waitingNums != 0:
                while doingNums < 20:
                    if self.breakFlag:
                        break
                    if waitingNums == 0:
                        break
                    if self.firstSync is None:
                        self.firstSync = time.time()
                    self.queueNum += 1
                    self.doing[self.queueNum] = self.waiting.pop(0)
                    self.doing[self.queueNum].doingKey = self.queueNum
                    self.doing[self.queueNum].doByThread()
                    doingNums = len(self.doing.keys())
                    waitingNums = len(self.waiting)
            else:
                break
        tryTime = 0
        while len(self.doing.keys()) > 0:
            tryTime += 1
            time.sleep(.5)
            if tryTime > 3:
                break
        try:
            if self.job.get('method') == 2 and self._allOperationsSuccessful():
                self.finalizeMove()
            self.commitSourceSnapshot()
            if self.finish:
                job_dao.add_job_task_item_many(self.finish, self._session_factory)
            self.updateTaskStatus()
        finally:
            self.jobClient.finishRun(self)

    def _allOperationsSuccessful(self):
        return (not self.breakFlag
                and self.sourceScanAttempted
                and not self.sourceScanFailed
                and all(item['status'] == 2 for item in self.finish))

    @staticmethod
    def normalizeRoot(path):
        path = str(path)
        return path if path.endswith('/') else path + '/'

    @staticmethod
    def entryLocation(rootPath, relativePath):
        rootPath = JobTask.normalizeRoot(rootPath)
        if '/' not in relativePath:
            return rootPath, relativePath
        parent, name = relativePath.rsplit('/', 1)
        return rootPath + parent + '/', name

    def finalizeMove(self):
        freshSourceDirectories = {}
        destinationRoots = {
            self.normalizeRoot(item) for item in self.job['dstPath'].split(':')
        }
        for entry in sorted(self.sourceSnapshot.values(), key=lambda item: item['path']):
            if self.breakFlag or entry['isDir'] or not self.fileSizeAllowed(entry['size']):
                continue
            srcPath, fileName = self.entryLocation(self.job['srcPath'], entry['path'])
            matching = [item for item in self.finish
                        if item['type'] == 2 and item['srcPath'] == srcPath
                        and item['fileName'] == fileName]
            expectedDestinations = {
                self.entryLocation(root, entry['path'])[0]
                for root in destinationRoots
            }
            deliveredDestinations = {
                self.normalizeRoot(item['dstPath'])
                for item in matching if item.get('dstPath')
            }
            if (len(matching) != len(deliveredDestinations)
                    or deliveredDestinations != expectedDestinations):
                self.markMoveDeleteFailure(
                    matching, srcPath, fileName, entry['size'], "移动目标不完整")
                continue
            try:
                if srcPath not in freshSourceDirectories:
                    _entries, details = self.readDirectory(srcPath, 0, 0)
                    freshSourceDirectories[srcPath] = details
                freshEntry = freshSourceDirectories[srcPath].get(fileName)
                freshSize = None if freshEntry is None else freshEntry.get('size')
            except Exception as e:
                self.markMoveDeleteFailure(matching, srcPath, fileName, entry['size'], str(e))
                continue
            if freshEntry is not None and (freshEntry.get('isDir') or freshSize != entry['size']):
                self.markMoveDeleteFailure(
                    matching, srcPath, fileName, entry['size'], "移动过程中源文件发生变化")
                continue
            if freshEntry is None:
                if not matching:
                    self.copyHook(srcPath, None, fileName, entry['size'], status=2, copyType=2)
                continue
            expectedFingerprint = entry.get('fingerprint')
            if expectedFingerprint is None:
                self.markMoveDeleteFailure(
                    matching, srcPath, fileName, entry['size'], "源文件版本信息缺失")
                continue
            if freshEntry.get('fingerprint') != expectedFingerprint:
                self.markMoveDeleteFailure(
                    matching, srcPath, fileName, entry['size'], "移动过程中源文件发生变化")
                continue
            try:
                self.alistClient.deleteFile(srcPath, [fileName], 0)
            except Exception as e:
                errMsg = "复制成功但删除源文件失败：{}".format(str(e))
                self.markMoveDeleteFailure(matching, srcPath, fileName, entry['size'], errMsg)
            else:
                if not matching:
                    self.copyHook(srcPath, None, fileName, entry['size'], status=2, copyType=2)
                appendMoved_file_safe(
                    self.job['id'], fileName, srcPath=srcPath,
                    session_factory=self._session_factory)
                self.movedFileNames.add(fileName)

    def markMoveDeleteFailure(self, matching, srcPath, fileName, fileSize, errMsg):
        if matching:
            for item in matching:
                item['status'] = 7
                item['errMsg'] = errMsg
        else:
            self.copyHook(srcPath, None, fileName, fileSize, status=7,
                          errMsg=errMsg, copyType=2)

    def commitSourceSnapshot(self):
        if not self._allOperationsSuccessful():
            return
        entries = list(self.sourceSnapshot.values())
        if self.job.get('method') == 2:
            entries = [entry for entry in entries
                       if entry['isDir'] or not self.fileSizeAllowed(entry['size'])]
        try:
            expectedIdentity = getattr(
                self, 'sourceSnapshotIdentity', job_dao.source_snapshot_identity(self.job))
            job_dao.replace_source_snapshot(
                self.job['id'], entries, expected_identity=expectedIdentity,
                session_factory=self._session_factory)
        except Exception as e:
            logger.exception(e)
            self.copyHook(self.normalizeRoot(self.job['srcPath']), None, None, None,
                          status=7, errMsg=str(e), isPath=1)

    def copyHook(self, srcPath, dstPath, name, size, alistTaskId=None, status=0,
                 errMsg=None, isPath=0, copyType=0, createTime=int(time.time())):
        self.finish.append({
            'taskId': self.taskId,
            'srcPath': srcPath,
            'dstPath': dstPath,
            'isPath': isPath,
            'fileName': name,
            'fileSize': size,
            'type': copyType,
            'alistTaskId': alistTaskId,
            'status': status,
            'errMsg': errMsg,
            'createTime': createTime,
        })

    def delHook(self, dstPath, name, size, status=2, errMsg=None, isPath=0,
                createTime=int(time.time())):
        self.finish.append({
            'taskId': self.taskId,
            'srcPath': None,
            'dstPath': dstPath,
            'isPath': isPath,
            'fileName': name,
            'fileSize': size,
            'type': 1,
            'alistTaskId': None,
            'status': status,
            'errMsg': errMsg,
            'createTime': createTime,
        })

    def sync(self):
        srcPath = self.normalizeRoot(self.job['srcPath'])
        jobExclude = self.job['exclude']
        spec = None
        if jobExclude is not None:
            spec = PathSpec.from_lines(GitWildMatchPattern, jobExclude.split(':'))
        dstPathList = [self.normalizeRoot(item) for item in self.job['dstPath'].split(':')]
        try:
            pathsOverlap = getattr(self.alistClient, 'pathsOverlap', virtual_paths_overlap)
            if any(pathsOverlap(srcPath, dstPath) for dstPath in dstPathList):
                raise ValueError("来源与目录存在重叠，已拒绝执行")
            storedSnapshot = job_dao.get_source_snapshot(
                self.job['id'], self._session_factory)
            if storedSnapshot['meta']['initialized'] == 1:
                self.previousSourceSnapshot = {
                    item['path']: item for item in storedSnapshot['entries']
                }
            else:
                self.previousSourceSnapshot = None
            if self.job.get('sourceMode') == 1 and storedSnapshot['meta']['initialized'] == 1:
                if self.scanSourceTree(srcPath, spec, srcPath):
                    self.syncFromSourceSnapshot(storedSnapshot['entries'], dstPathList)
            else:
                for index, dstItem in enumerate(dstPathList):
                    self.syncWithHave(srcPath, dstItem, spec, srcPath, dstItem, index == 0)
        except Exception as e:
            logger.exception(e)
            self.sourceScanFailed = True
            self.copyHook(srcPath, None, None, None, status=7, errMsg=str(e), isPath=1)
        finally:
            self.scanFinish = True

    def scanSourceTree(self, path, spec, rootPath):
        if self.breakFlag:
            return False
        try:
            entries = self.listDir(path, True, spec, rootPath)
        except Exception:
            return False
        for name in entries:
            if name.endswith('/') and not self.scanSourceTree(path + name, spec, rootPath):
                return False
        return not self.breakFlag and not self.sourceScanFailed

    def syncFromSourceSnapshot(self, storedEntries, dstPathList):
        previous = {
            item['path']: {
                'path': item['path'], 'isDir': int(item['isDir']),
                'size': item['size'], 'fingerprint': item.get('fingerprint'),
            } for item in storedEntries
        }
        current = self.sourceSnapshot
        changedFiles = [entry for path, entry in current.items()
                        if not entry['isDir']
                        and self.fileSizeAllowed(entry['size'])
                        and (self.job['method'] == 2
                             or self.sourceEntryChanged(previous.get(path), entry))]
        newDirectories = [entry for path, entry in current.items()
                          if entry['isDir']
                          and (path not in previous or not previous[path]['isDir'])]
        removed = [entry for path, entry in previous.items()
                   if path not in current or current[path]['isDir'] != entry['isDir']]
        for dstRoot in dstPathList:
            failedDirectoryPrefixes = []
            if self.job['method'] == 1:
                self.deleteSnapshotEntries(dstRoot, removed)
            for entry in sorted(newDirectories, key=lambda item: (item['path'].count('/'), item['path'])):
                if any(self.pathWithin(entry['path'], prefix) for prefix in failedDirectoryPrefixes):
                    continue
                dstPath = dstRoot + entry['path'] + '/'
                srcPath = self.normalizeRoot(self.job['srcPath']) + entry['path'] + '/'
                status = 2
                errMsg = None
                try:
                    self.alistClient.mkdir(dstPath, self.job['scanIntervalT'])
                except Exception as e:
                    status = 7
                    errMsg = str(e)
                    failedDirectoryPrefixes.append(entry['path'])
                self.copyHook(srcPath, dstPath, None, None, status=status, errMsg=errMsg, isPath=1)
            for entry in changedFiles:
                parentPath, fileName = self.entryLocation(dstRoot, entry['path'])
                if any(self.pathWithin(entry['path'], prefix) for prefix in failedDirectoryPrefixes):
                    continue
                srcPath, _ = self.entryLocation(self.job['srcPath'], entry['path'])
                self.copyFile(srcPath, parentPath, fileName, entry['size'])

    @staticmethod
    def pathWithin(path, prefix):
        return path == prefix or path.startswith(prefix + '/')

    @staticmethod
    def sourceEntryChanged(previous, current):
        if (previous is None or previous.get('isDir')
                or previous.get('size') != current.get('size')):
            return True
        previousFingerprint = previous.get('fingerprint')
        currentFingerprint = current.get('fingerprint')
        return ((previousFingerprint is not None or currentFingerprint is not None)
                and previousFingerprint != currentFingerprint)

    def sourceFileChangedSinceSnapshot(self, srcPath, srcRootPath, fileName):
        previous = getattr(self, 'previousSourceSnapshot', None)
        if previous is None:
            return False
        relative_base = (
            srcPath[len(srcRootPath):].strip('/') if srcPath.startswith(srcRootPath) else '')
        relativePath = '/'.join(item for item in (relative_base, fileName) if item)
        current = self.sourceSnapshot.get(relativePath)
        previous_entry = previous.get(relativePath)
        return (current is not None and previous_entry is not None
                and self.sourceEntryChanged(previous_entry, current))

    def deleteSnapshotEntries(self, dstRoot, removedEntries):
        for entry in removedEntries:
            if entry['isDir'] or not self.fileSizeAllowed(entry['size']):
                continue
            parentPath, name = self.entryLocation(dstRoot, entry['path'])
            self.delFile(parentPath, name, entry['size'])

    def copyFile(self, srcPath, dstPath, fileName, fileSize):
        if self.breakFlag:
            return
        if self.job['method'] == 2 and fileName in self.movedFileNames:
            logger.info("跳过已移动文件：%s（源：%s）", fileName, srcPath)
            self.copyHook(srcPath, dstPath, fileName, fileSize, status=2, copyType=2,
                          errMsg="该文件已移动过（记录于移动日志）")
            return
        copyItem = CopyItem(srcPath, dstPath, fileName, fileSize, self.job['method'], self)
        self.waiting.append(copyItem)

    def hasFileSizeFilter(self):
        return self.job.get('minFileSize') is not None or self.job.get('maxFileSize') is not None

    def fileSizeAllowed(self, fileSize):
        return is_file_size_allowed(file_size=fileSize, min_file_size=self.job.get('minFileSize'),
                                    max_file_size=self.job.get('maxFileSize'))

    def delFile(self, path, fileName, size):
        if self.breakFlag:
            return
        isPath = fileName.endswith('/')
        status = 2
        errMsg = None
        createTime = int(time.time())
        try:
            self.alistClient.deleteFile(
                path, [fileName if not isPath else fileName[:-1]], self.job['scanIntervalT'])
        except Exception as e:
            status = 7
            errMsg = str(e)
        self.delHook(path, fileName, None if isPath else size, status, errMsg, isPath, createTime)

    def listDir(self, path, firstDst, spec, rootPath, isSrc=True):
        useCache = 1 if isSrc and not firstDst else self.job["useCache{}".format('S' if isSrc else 'T')]
        scanInterval = self.job["scanInterval{}".format('S' if isSrc else 'T')]
        try:
            entries, details = self.readDirectory(path, useCache, scanInterval, spec, rootPath)
            if isSrc and firstDst:
                self.recordSourceEntries(path, rootPath, entries, details)
            return entries
        except Exception as e:
            errMsg = "扫描{}目录出错：{}".format('来源' if isSrc else '目标', str(e))
            logger.error(errMsg)
            logger.exception(e)
            if isSrc and firstDst:
                self.sourceScanAttempted = True
                self.sourceScanFailed = True
            self.copyHook(path if isSrc else None, None if isSrc else path, None, None,
                          status=7, errMsg=errMsg, isPath=1)
            raise e

    def readDirectory(self, path, useCache=0, scanInterval=0, spec=None, rootPath=None):
        detailApi = getattr(self.alistClient, 'fileListDetailApi', None)
        if callable(detailApi):
            rawDetails = detailApi(path, useCache, scanInterval, spec, rootPath)
            details = {}
            entries = {}
            for name, rawDetail in rawDetails.items():
                detail = rawDetail if isinstance(rawDetail, dict) else {}
                isDirectory = bool(detail.get('isDir', name.endswith('/')))
                size = None if isDirectory else detail.get('size')
                details[name] = {
                    'isDir': 1 if isDirectory else 0,
                    'size': size,
                    'fingerprint': detail.get('fingerprint'),
                }
                entries[name] = {} if isDirectory else size
            return entries, details
        entries = self.alistClient.fileListApi(path, useCache, scanInterval, spec, rootPath)
        details = {
            name: {
                'isDir': 1 if name.endswith('/') else 0,
                'size': None if name.endswith('/') else size,
                'fingerprint': None,
            } for name, size in entries.items()
        }
        return entries, details

    def recordSourceEntries(self, path, rootPath, entries, details=None):
        self.sourceScanAttempted = True
        relativeBase = path[len(rootPath):].strip('/') if path.startswith(rootPath) else ''
        for name, size in entries.items():
            isDirectory = name.endswith('/')
            cleanName = name[:-1] if isDirectory else name
            relativePath = '/'.join(item for item in (relativeBase, cleanName) if item)
            entry = {
                'path': relativePath,
                'isDir': 1 if isDirectory else 0,
                'size': None if isDirectory else size,
            }
            fingerprint = (details or {}).get(name, {}).get('fingerprint')
            if fingerprint is not None:
                entry['fingerprint'] = fingerprint
            self.sourceSnapshot[relativePath] = entry

    def deleteTargetOnlyDir(self, dstPath, spec, dstRootPath, firstDst):
        if self.breakFlag:
            return
        try:
            dstFiles = self.listDir(dstPath, firstDst, spec, dstRootPath, False)
        except Exception:
            return
        for key, size in dstFiles.items():
            if self.breakFlag:
                return
            if key.endswith('/'):
                self.deleteTargetOnlyDir(dstPath + key, spec, dstRootPath, firstDst)
            elif self.fileSizeAllowed(size):
                self.delFile(dstPath, key, size)

    def syncWithHave(self, srcPath, dstPath, spec, srcRootPath, dstRootPath, firstDst):
        if self.breakFlag:
            return
        try:
            srcFiles = self.listDir(srcPath, firstDst, spec, srcRootPath)
            dstFiles = self.listDir(dstPath, firstDst, spec, dstRootPath, False)
        except Exception:
            return
        for key in srcFiles.keys():
            if not key.endswith('/'):
                if not self.fileSizeAllowed(srcFiles[key]):
                    continue
                if (self.job['method'] == 2
                        or self.sourceFileChangedSinceSnapshot(srcPath, srcRootPath, key)
                        or key not in dstFiles or dstFiles[key] != srcFiles[key]):
                    self.copyFile(srcPath, dstPath, key, srcFiles[key])
            else:
                if key not in dstFiles:
                    self.syncWithOutHave(srcPath + key, dstPath + key, spec, srcRootPath,
                                         dstRootPath, firstDst)
                else:
                    self.syncWithHave(srcPath + key, dstPath + key, spec, srcRootPath,
                                      dstRootPath, firstDst)
        if self.job['method'] == 1:
            for dstKey in dstFiles.keys():
                if dstKey not in srcFiles:
                    if dstKey.endswith('/') and self.hasFileSizeFilter():
                        self.deleteTargetOnlyDir(dstPath + dstKey, spec, dstRootPath, firstDst)
                    elif dstKey.endswith('/') or self.fileSizeAllowed(dstFiles[dstKey]):
                        self.delFile(dstPath, dstKey, dstFiles[dstKey])

    def syncWithOutHave(self, srcPath, dstPath, spec, srcRootPath, dstRootPath, firstDst):
        if self.breakFlag:
            return
        status = 2
        errMsg = None
        try:
            self.alistClient.mkdir(dstPath, self.job['scanIntervalT'])
        except Exception as e:
            status = 7
            errMsg = str(e)
        self.copyHook(srcPath, dstPath, None, None, status=status, errMsg=errMsg, isPath=1)
        if status != 2:
            return
        try:
            srcFiles = self.listDir(srcPath, firstDst, spec, srcRootPath)
        except Exception:
            return
        for key in srcFiles.keys():
            if self.breakFlag:
                break
            if key.endswith('/'):
                self.syncWithOutHave(srcPath + key, dstPath + key, spec, srcRootPath,
                                      dstRootPath, firstDst)
            elif self.fileSizeAllowed(srcFiles[key]):
                self.copyFile(srcPath, dstPath, key, srcFiles[key])

    def updateTaskStatus(self):
        from core.sync import task_service
        self.getCurrent()
        failOrOtherNum = len(self.currentTasks[7]) + len(self.currentTasks[-1])
        status = 7 if self.breakFlag else 2 if failOrOtherNum == 0 else 3
        task_service.update_job_task_status(
            self.taskId, status, task_list=self.currentTasks,
            create_time=self.createTime, notifier=self._notifier,
            session_factory=self._session_factory)


def append_moved_file_safe(job_id, file_name, src_path=None, session_factory=None):
    try:
        append_moved_file(job_id, file_name, src_path=src_path, session_factory=session_factory)
    except Exception as e:
        logger.exception(e)


class JobClient:
    def __init__(self, job, isInit=False, notifier=None, session_factory=None):
        addJobId = 0
        if 'enable' not in job:
            job['enable'] = 1
        if 'method' not in job:
            job['method'] = 0
        if 'id' not in job:
            addJobId = job_dao.add_job(job, session_factory)
            job = job_dao.get_job_by_id(addJobId, session_factory)
        self.jobId = job['id']
        self.job = job
        self._notifier = notifier
        self._session_factory = session_factory
        self.scheduled = None
        self.scheduledJob = None
        self.jobDoing = False
        self.runLock = threading.Lock()
        self.currentJobTask = None
        try:
            self.doByTime()
        except Exception as e:
            if isInit or addJobId != 0:
                logger.error("添加同步作业过程出错：%s", json_dumps(job))
                job_dao.delete_job(self.jobId, session_factory)
            raise e

    def doJob(self, lockAcquired=False):
        if not lockAcquired and not self.runLock.acquire(blocking=False):
            return
        self.jobDoing = True
        taskId = None
        try:
            taskId = job_dao.add_job_task({
                'jobId': self.jobId,
                'runTime': int(time.time()),
            }, self._session_factory)
            if self.job['enable'] == 0:
                raise Exception("abort")
            task = JobTask(taskId, self, self._notifier, self._session_factory)
            self.currentJobTask = task
            task.start()
        except Exception as e:
            self.finishRun()
            errMsg = "执行同步作业出错：{}".format(str(e))
            logger.error(errMsg)
            if taskId is not None:
                from core.sync import task_service
                task_service.update_job_task_status(
                    taskId, 6, errMsg, notifier=self._notifier,
                    session_factory=self._session_factory)
            logger.exception(e)

    def doManual(self):
        if not self.runLock.acquire(blocking=False):
            raise Exception("作业正在运行中")
        self.jobDoing = True
        doJobThread = threading.Thread(
            target=self.doJob, kwargs={'lockAcquired': True})
        doJobThread.start()

    def finishRun(self, task=None):
        if task is None or self.currentJobTask is task:
            self.currentJobTask = None
        self.jobDoing = False
        if self.runLock.locked():
            try:
                self.runLock.release()
            except RuntimeError:
                pass

    def doByTime(self):
        params = {
            'func': self.doJob,
            'misfire_grace_time': 15 * 60,
            'trigger': 'interval' if self.job['isCron'] == 0 else 'cron',
        }
        if self.job['isCron'] == 0:
            interval = self.job['interval']
            if interval is not None and str(interval).strip() != '':
                params['minutes'] = interval
            else:
                raise Exception("同步间隔丢失")
        elif self.job['isCron'] == 1:
            flag = 0
            for item in ['year', 'month', 'day', 'week', 'day_of_week', 'hour', 'minute',
                         'second', 'start_date', 'end_date']:
                if item in self.job and self.job[item] is not None and self.job[item] != '':
                    flag += 1
                    params[item] = self.job[item]
            if flag == 0:
                raise Exception("cron 配置丢失")
        else:
            return
        self.scheduled = BackgroundScheduler()
        self.scheduledJob = self.scheduled.add_job(**params)
        self.scheduled.start()
        if self.job['enable'] == 0:
            self.scheduledJob.pause()

    def resumeJob(self):
        if self.scheduledJob is None:
            raise Exception("无法恢复已丢失调度的作业")
        job_dao.update_job_enable(self.jobId, 1, self._session_factory)
        self.job['enable'] = 1
        self.scheduledJob.resume()

    def abortJob(self):
        if self.currentJobTask:
            self.currentJobTask.breakFlag = True

    def stopJob(self, remove=False):
        self.job['enable'] = 0
        if self.currentJobTask:
            self.currentJobTask.breakFlag = True
        if remove:
            if self.scheduled is not None:
                try:
                    self.scheduled.shutdown(wait=False)
                except Exception as e:
                    logger.warning("停止同步作业调度失败：%s", str(e))
                self.scheduled = None
        else:
            if self.scheduledJob is not None:
                try:
                    self.scheduledJob.pause()
                except Exception as e:
                    logger.warning("禁用同步作业调度失败：%s", str(e))
        if not remove:
            job_dao.update_job_enable(self.jobId, 0, self._session_factory)
            job_dao.update_job_task_status_by_status_and_job_id(
                self.jobId, self._session_factory)


def json_dumps(job):
    import json
    return json.dumps(job, ensure_ascii=False, default=str)
