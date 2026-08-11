import subprocess, sys, os
os.chdir(r"d:\项目\阿里云盘频道监控转存\aliyundrive-sub")

# 1) 添加所有变更（含删除）
r1 = subprocess.run(["git", "add", "-A"], capture_output=True, text=True)
if r1.returncode != 0:
    print("add failed:", r1.stderr)
    sys.exit(1)

# 2) 提交
msg = (
    "refactor: 彻底移除同步管理模块（同步作业/同步记录/存储目录）\n\n"
    "- 删除 core/sync/、core/sync_storage/ 整目录与 models_sync.py\n"
    "- 删除同步蓝图（sync_bp / storage_picker_bp / sync_records_bp）及其模板与静态资源\n"
    "- 清理引用：web/app.py 去蓝图注册、web/services.py 去 sync_service 字段、\n"
    "  根 app.py 去 SyncService 装配与 _init_sync_module、db.py 去 import models_sync、\n"
    "  requirements.txt 去 pathspec、base.html 去三个同步导航项、style.css 去 .sync-items 样式\n"
    "- 验证：全量 py_compile 0 错误；其余路由 200，旧 /sync /sync-storage /sync-records 返回 404"
)
r2 = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
print("commit rc=", r2.returncode)
print(r2.stdout)
if r2.stderr:
    print("stderr:", r2.stderr)
if r2.returncode != 0:
    sys.exit(r2.returncode)

# 3) 推送
r3 = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)
print("push rc=", r3.returncode)
print(r3.stdout)
if r3.stderr:
    print("stderr:", r3.stderr)
sys.exit(r3.returncode)
