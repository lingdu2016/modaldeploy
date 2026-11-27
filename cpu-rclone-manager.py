import os
import subprocess
import threading
import time
import socket
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "rclone-manager"
VOLUME_DATA_NAME = "rclone-manager-data"      # 本项目自己的 /data（应用数据、rclone 二进制、日志）
VOLUME_CONFIG_NAME = "rclone-manager-config"  # 本项目自己的 /config（rclone.conf）

# 官方 Docker 镜像（debian:bookworm-slim 基础，纯 Web Server）
# 想固定版本：把 latest 改成具体 tag
IMAGE_NAME = "ghcr.io/zarestia-dev/rclone-manager:latest"

# 镜像里的 server 二进制路径（来自官方 Dockerfile）
SERVER_BINARY_PATH = "/usr/local/bin/rclone-manager-server"

# Web 端口
WEB_PORT = 8080

# 本项目自己的目录（与官方镜像保持一致）
DATA_DIR = "/data"
CONFIG_DIR = "/config"
LOGS_DIR = "/data/logs"
CACHE_DIR = "/tmp/rclone-manager-cache"   # 浏览器上传的文件先暂存在这里，放 /tmp 避免占用 Volume

# ---------- 想在 RClone Manager 里查看的其他 Volume（只写名字） ----------
# 每个名字会挂载到 /mnt/<名字>，之后在 UI 里新建 Local 类型的 remote，浏览 /mnt 即可看到。
# 注意：
#   - 名字必须和 Modal 上的 Volume 名完全一致，写错会导致部署/启动失败
#   - 不要写本项目自己的 rclone-manager-data 和 rclone-manager-config（已经挂在 /data、/config）
#   - vaultwarden-data 是密码库数据，非常敏感；只想看别的就把这一行删掉
EXTRA_VOLUME_NAMES = [
    "slash-data",
    "s2-data",
    "qwen2api-data",
    # "s2-data",
]
EXTRA_MOUNT_ROOT = "/mnt"

# 是否只读挂载其他 Volume：
#   True  -> 只能看、下载，不能删除/修改（推荐，防止误删线上服务的数据）
#   False -> 可读写（UI 里删文件会真的删掉；多个容器同时写同一个 Volume 是"后写覆盖"）
EXTRA_VOLUMES_READ_ONLY = True

MAX_CONTAINERS = 1          # 配置和状态存本地文件，必须单实例，避免多实例写冲突
MAX_CONCURRENT_INPUTS = 32
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 180
WEB_STARTUP_TIMEOUT_SECONDS = 180

CPU = 0.25
MEMORY_MB = 256

# 提交本项目 Volume / 刷新其他 Volume 的间隔（秒）
VOLUME_COMMIT_INTERVAL_SECONDS = 30

# Modal Secret 名称，里面需要三项：
#   RCLONE_MANAGER_USER     Web 登录用户名
#   RCLONE_MANAGER_PASS     Web 登录密码
#   RCLONE_MANAGER_SECRET   加密凭据库的主密码（设好后不要改，改了旧的加密内容就解不开）
SECRET_NAME = "rclone-manager-secret"
# ====================================================


# ====================================================
# 1. 本项目自己的持久化数据卷
# ====================================================
vol_data = modal.Volume.from_name(
    VOLUME_DATA_NAME,
    create_if_missing=True,
)
vol_config = modal.Volume.from_name(
    VOLUME_CONFIG_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 其他要查看的 Volume（按清单生成）
# ====================================================
def make_extra_volume(name):
    """按名字取得已存在的 Volume；不存在时不会自动创建（避免写错名字多出空 Volume）。"""
    volume = modal.Volume.from_name(name)
    if EXTRA_VOLUMES_READ_ONLY:
        try:
            return volume.read_only()   # 只读挂载
        except AttributeError:
            # 你的 modal 版本太旧，不支持 read_only()，请升级：pip install -U modal
            raise RuntimeError(
                "当前 modal 版本不支持 Volume.read_only()，请升级 modal，"
                "或把 EXTRA_VOLUMES_READ_ONLY 改为 False。"
            )
    return volume


# 形如 {"/mnt/webdav-data": <Volume>, ...}
extra_volumes = {
    f"{EXTRA_MOUNT_ROOT}/{name}": make_extra_volume(name)
    for name in EXTRA_VOLUME_NAMES
}


# ====================================================
# 3. 镜像：直接使用官方镜像
# ====================================================
# add_python 是为了让 Modal 能在这个镜像里运行 Python 函数。
# entrypoint([]) 清掉官方 entrypoint.sh（它只做 PUID/PGID 降权，Modal 里不需要）。
image = modal.Image.from_registry(
    IMAGE_NAME,
    add_python="3.12",
).entrypoint([])

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 4. 辅助：后台线程
# ====================================================
def commit_volumes_periodically():
    """后台线程：把本项目写入的数据提交到自己的 Volume，防止容器回收时丢数据。"""
    while True:
        time.sleep(VOLUME_COMMIT_INTERVAL_SECONDS)
        for name, volume in (("data", vol_data), ("config", vol_config)):
            try:
                volume.commit()
            except Exception as error:
                print(f"Volume({name}) 提交失败：{error}", flush=True)


def reload_extra_volumes_periodically():
    """后台线程：定期刷新其他 Volume，这样别的程序新写入的文件才能在这里看到。
    （其他 Volume 只刷新、不提交，避免覆盖别人的写入。）"""
    while True:
        time.sleep(VOLUME_COMMIT_INTERVAL_SECONDS)
        for path, volume in extra_volumes.items():
            try:
                volume.reload()
            except Exception as error:
                # 有文件正被打开时 reload 可能失败，忽略，下个周期再试
                print(f"Volume({path}) 刷新失败：{error}", flush=True)


# ====================================================
# 5. 启动 RClone Manager Server
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={
        DATA_DIR: vol_data,
        CONFIG_DIR: vol_config,
        **extra_volumes,        # 展开清单里的所有其他 Volume
    },
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    """启动 RClone Manager（rclone 的 Web 管理界面）。"""
    print("======================================", flush=True)
    print("正在启动 RClone Manager...", flush=True)
    print("======================================", flush=True)

    # ---------- 读取并检查 Secret ----------
    user = os.environ.get("RCLONE_MANAGER_USER", "").strip()
    password = os.environ.get("RCLONE_MANAGER_PASS", "").strip()
    master_secret = os.environ.get("RCLONE_MANAGER_SECRET", "").strip()

    # 不设账号密码 = 公网公开，别人可以直接操作你的网盘和 Volume，所以这里强制要求
    if not user or not password:
        raise ValueError(
            "RCLONE_MANAGER_USER 和 RCLONE_MANAGER_PASS 未设置，请在 Modal Secret 中配置。"
        )

    # 没有主密码，加密凭据只存内存，容器重启后全部丢失
    if not master_secret:
        raise ValueError(
            "RCLONE_MANAGER_SECRET 未设置，请在 Modal Secret 中配置。"
        )

    # ---------- 准备目录 ----------
    for path in (DATA_DIR, CONFIG_DIR, LOGS_DIR, CACHE_DIR):
        os.makedirs(path, exist_ok=True)

    # ---------- 配置环境变量（变量名来自官方 Dockerfile） ----------
    env = os.environ.copy()
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")
    env["RCLONE_MANAGER_HOST"] = "0.0.0.0"
    env["RCLONE_MANAGER_PORT"] = str(WEB_PORT)
    env["RCLONE_MANAGER_USER"] = user
    env["RCLONE_MANAGER_PASS"] = password
    env["RCLONE_MANAGER_SECRET"] = master_secret
    env["RCLONE_MANAGER_DATA_DIR"] = DATA_DIR
    env["RCLONE_MANAGER_LOG_DIR"] = LOGS_DIR
    env["RCLONE_MANAGER_CACHE_DIR"] = CACHE_DIR
    env["RCLONE_MANAGER_INSTALLATION_TYPE"] = "docker"
    env["RCLONE_CONFIG"] = f"{CONFIG_DIR}/rclone.conf"
    env["RCLONE_LOCAL_COPY_LINKS"] = "true"   # 让 rclone 的 Local 后端跟随符号链接，/mnt 下才能看到各 Volume

    print(f"二进制：{SERVER_BINARY_PATH}", flush=True)
    print(f"用户名：{user}（长度 {len(user)}），密码长度：{len(password)}", flush=True)
    print(f"数据目录：{DATA_DIR}", flush=True)
    print(f"rclone 配置：{CONFIG_DIR}/rclone.conf", flush=True)
    print(f"Web 端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)
    print(
        f"额外 Volume（{'只读' if EXTRA_VOLUMES_READ_ONLY else '可读写'}）：",
        ", ".join(extra_volumes.keys()) or "无",
        flush=True,
    )

    # ---------- 启动 server ----------
    process = subprocess.Popen([SERVER_BINARY_PATH], env=env)
    print(f"进程已启动，PID：{process.pid}", flush=True)

    # ---------- 后台线程：提交自己的 Volume、刷新其他 Volume ----------
    threading.Thread(target=commit_volumes_periodically, daemon=True).start()
    threading.Thread(target=reload_extra_volumes_periodically, daemon=True).start()

    # ---------- 等待端口开始监听 ----------
    start_time = time.monotonic()
    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"RClone Manager 启动失败，退出代码：{return_code}")

        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"RClone Manager 已就绪，监听端口：{WEB_PORT}", flush=True)
                return
        except OSError:
            time.sleep(0.5)

    raise RuntimeError("RClone Manager 启动超时，请检查 Modal 容器日志")
