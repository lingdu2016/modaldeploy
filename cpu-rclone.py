import os
import subprocess
import time
import socket
import threading
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "rclone"

# ---------- Modal Volume 配置 ----------

# rclone 自己的配置 Volume（保存 rclone.conf，不参与业务数据备份）
VOLUME_NAME = "rclone-data"

# 需要备份的 Modal Volume
# 左侧：Modal Volume 的真实名称
# 右侧：Koofr 中对应的备份目录名称
SOURCE_VOLUMES = {
    "qwen2api-data": "qwen2api-data",
    "slash-data": "slash-data",
}

# ---------- 官方镜像 ----------
IMAGE = "rclone/rclone:1.75.0"

# ---------- Web 服务配置 ----------
WEB_PORT = 5572
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 180

# Web GUI 容器内定期 reload 业务 Volume 的间隔（秒）
# 用于让长期运行的 Web 容器看到其他容器新写入并提交的文件
WEB_RELOAD_INTERVAL_SECONDS = 60

# ---------- Web 服务资源 ----------
CPU = 0.25
MEMORY_MB = 256

# ---------- 自动备份资源 ----------
BACKUP_CPU = 0.25
BACKUP_MEMORY_MB = 512
BACKUP_TIMEOUT_SECONDS = 21600  # 6 小时

# 备份失败后的自动重试
BACKUP_MAX_RETRIES = 2
BACKUP_RETRY_DELAY_SECONDS = 300.0

# ---------- 自动备份时间 ----------
BACKUP_CRON = "30 14 * * *"  # 每天北京时间 13:30
BACKUP_TIMEZONE = "Asia/Shanghai"

# ---------- Koofr 备份配置 ----------
BACKUP_ROOT = "koofr:Modal-Backups"

# ---------- 本地 Volume 挂载路径 ----------
SOURCE_ROOT = "/mnt/sources"

# ---------- Modal Secret ----------
# 需要包含：RCLONE_WEB_USER、RCLONE_WEB_PASS
SECRET_NAME = "rclone-secret"

# ---------- rclone 持久化目录 ----------
CONFIG_DIR = "/config"
CACHE_DIR = "/cache"
CONFIG_FILE = "/config/rclone.conf"

# ---------- 备份传输参数 ----------
BACKUP_TRANSFERS = 2
BACKUP_CHECKERS = 4

# ====================================================


# ====================================================
# 1. 引用 Modal Volumes
# ====================================================

config_volume = modal.Volume.from_name(VOLUME_NAME)

# 业务 Volume：只读挂载，rclone 无法修改源数据
source_volume_objects = {
    volume_name: modal.Volume.from_name(volume_name).with_mount_options(
        read_only=True
    )
    for volume_name in SOURCE_VOLUMES
}

# qwen2api-data -> /mnt/sources/qwen2api-data
SOURCE_MOUNT_PATHS = {
    f"{SOURCE_ROOT}/{volume_name}": volume
    for volume_name, volume in source_volume_objects.items()
}

# Web GUI：配置可读写，业务 Volume 只读
WEB_VOLUMES = {
    CONFIG_DIR: config_volume,
    **SOURCE_MOUNT_PATHS,
}

# 备份 / 调试任务：配置也只读
BACKUP_VOLUMES = {
    CONFIG_DIR: config_volume.with_mount_options(read_only=True),
    **SOURCE_MOUNT_PATHS,
}


# ====================================================
# 2. 官方镜像 + 安装 Python
# ====================================================

image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 官方镜像 ENTRYPOINT 是 rclone，这里改为直接执行传入的命令
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


# ====================================================
# 3. 创建 Modal App
# ====================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 4. 公共工具函数
# ====================================================

def build_env():
    """构建 rclone 子进程使用的环境变量。"""
    env = os.environ.copy()
    env["TZ"] = BACKUP_TIMEZONE
    env["RCLONE_CONFIG"] = CONFIG_FILE
    return env


def list_source_dir(path):
    """返回目录下的文件信息，用于日志和调试。"""
    items = []
    for entry in sorted(os.listdir(path)):
        full = os.path.join(path, entry)
        try:
            st = os.stat(full)
            kind = "dir" if os.path.isdir(full) else "file"
            items.append(f"{kind:4} {st.st_size:>12} {entry}")
        except OSError as e:
            items.append(f"?    {entry} ({e})")
    return items


def run_backup():
    print("======================================", flush=True)
    print("开始执行 Modal → Koofr 备份", flush=True)
    print("======================================", flush=True)

    # 4.1 检查 rclone 配置文件
    if not os.path.isfile(CONFIG_FILE):
        raise FileNotFoundError(f"找不到 rclone 配置文件：{CONFIG_FILE}")

    env = build_env()

    # 4.2 检查 Koofr Remote
    result = subprocess.run(
        ["rclone", "listremotes"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    if "koofr:" not in result.stdout.splitlines():
        raise RuntimeError("没有找到 koofr: Remote，请检查 rclone.conf。")

    print("Koofr 配置检查成功。", flush=True)

    # 4.3 逐个备份业务 Volume
    errors = []

    for volume_name, backup_folder in SOURCE_VOLUMES.items():
        source_path = f"{SOURCE_ROOT}/{volume_name}"
        destination_path = f"{BACKUP_ROOT}/{backup_folder}"

        print("--------------------------------------", flush=True)
        print(f"备份项目：{volume_name}", flush=True)
        print(f"源目录：{source_path}", flush=True)
        print(f"目标目录：{destination_path}", flush=True)

        if not os.path.isdir(source_path):
            message = f"源目录不存在或未挂载：{source_path}"
            print(message, flush=True)
            errors.append(message)
            continue

        # 备份前打印源目录内容，方便确认挂载是否正确
        entries = list_source_dir(source_path)
        print(f"源目录当前内容（{len(entries)} 项）：", flush=True)
        for line in entries:
            print(f"  {line}", flush=True)

        if not entries:
            message = f"警告：{source_path} 是空目录，请确认 Volume 名称和环境是否正确"
            print(message, flush=True)
            errors.append(message)
            continue

        # rclone copy：只复制，不删除目标端已有文件
        command = [
            "rclone",
            "copy",
            source_path,
            destination_path,
            "--create-empty-src-dirs",
            "--transfers", str(BACKUP_TRANSFERS),
            "--checkers", str(BACKUP_CHECKERS),
            "--retries", "3",
            "--low-level-retries", "10",
            "--stats", "30s",
            "--stats-one-line",
            "--log-level", "INFO",
        ]

        try:
            subprocess.run(command, env=env, check=True)
            print(f"{volume_name} 备份成功。", flush=True)

        except subprocess.CalledProcessError as e:
            message = f"{volume_name} 备份失败，退出代码：{e.returncode}"
            print(message, flush=True)
            errors.append(message)

    # 4.4 汇总结果
    print("======================================", flush=True)

    if errors:
        print("备份存在错误：", flush=True)
        for error in errors:
            print(error, flush=True)
        raise RuntimeError(f"备份完成，但有 {len(errors)} 个问题。")

    print("所有业务 Volume 均已成功备份到 Koofr。", flush=True)
    print("======================================", flush=True)


def reload_worker():
    """
    定期 reload 业务 Volume。
    Web GUI 容器长期运行，挂载内容是启动时的快照，
    reload 后才能看到其他容器之后提交的新文件。
    """
    while True:
        time.sleep(WEB_RELOAD_INTERVAL_SECONDS)

        for volume_name, volume in source_volume_objects.items():
            try:
                volume.reload()
            except Exception as e:
                print(f"Volume reload 失败（{volume_name}）：{e}", flush=True)


# ====================================================
# 5. Rclone Web GUI 服务
# ====================================================

@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes=WEB_VOLUMES,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    print("======================================", flush=True)
    print("正在启动 Rclone Web GUI...", flush=True)
    print("======================================", flush=True)

    # 5.1 检查 Web 身份验证 Secret
    web_user = os.environ.get("RCLONE_WEB_USER", "").strip()
    web_pass = os.environ.get("RCLONE_WEB_PASS", "").strip()

    if not web_user:
        raise ValueError("RCLONE_WEB_USER 未设置，请检查 Modal Secret。")

    if not web_pass:
        raise ValueError("RCLONE_WEB_PASS 未设置，请检查 Modal Secret。")

    if len(web_pass) < 12:
        raise ValueError("RCLONE_WEB_PASS 不能少于 12 个字符。")

    # 5.2 创建目录
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 5.3 环境变量
    # 账号密码通过环境变量传入，避免出现在进程命令行里
    env = build_env()
    env["RCLONE_RC_USER"] = web_user
    env["RCLONE_RC_PASS"] = web_pass

    # 5.4 启动前打印业务 Volume 的实际内容，便于排查
    for volume_name in SOURCE_VOLUMES:
        path = f"{SOURCE_ROOT}/{volume_name}"
        if os.path.isdir(path):
            print(f"{path} 内容：{os.listdir(path)}", flush=True)
        else:
            print(f"{path} 不存在，Volume 可能未挂载。", flush=True)

    # 5.5 后台定期 reload 业务 Volume
    threading.Thread(target=reload_worker, daemon=True).start()

    # 5.6 Rclone Web GUI 启动命令
    command = [
        "rclone",
        "rcd",
        "--rc-addr", f"0.0.0.0:{WEB_PORT}",
        "--rc-web-gui",
        "--rc-web-gui-no-open-browser",
        "--cache-dir", CACHE_DIR,
        "--log-level", "INFO",
    ]

    process = subprocess.Popen(command, env=env)
    print(f"Rclone 进程已启动，PID：{process.pid}", flush=True)

    # 5.7 等待服务端口就绪
    start_time = time.monotonic()

    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        exit_code = process.poll()

        if exit_code is not None:
            raise RuntimeError(f"Rclone 启动失败，退出代码：{exit_code}")

        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"Rclone 已成功启动，监听端口：{WEB_PORT}", flush=True)
                break
        except OSError:
            time.sleep(0.5)

    else:
        raise RuntimeError("Rclone 启动超时，请检查 Modal 容器日志。")

    print("Rclone Web GUI 服务已就绪。", flush=True)


# ====================================================
# 6. 每天北京时间 13:30 自动备份
# ====================================================

@app.function(
    schedule=modal.Cron(BACKUP_CRON, timezone=BACKUP_TIMEZONE),
    retries=modal.Retries(
        max_retries=BACKUP_MAX_RETRIES,
        initial_delay=BACKUP_RETRY_DELAY_SECONDS,
    ),
    cpu=BACKUP_CPU,
    memory=BACKUP_MEMORY_MB,
    timeout=BACKUP_TIMEOUT_SECONDS,
    max_containers=1,
    volumes=BACKUP_VOLUMES,
)
def daily_backup():
    print("Modal 定时备份任务启动（北京时间每天 13:30）", flush=True)
    run_backup()


# ====================================================
# 7. 手动备份入口
# ====================================================
# 运行：modal run rclone_modal.py::manual_backup

@app.function(
    cpu=BACKUP_CPU,
    memory=BACKUP_MEMORY_MB,
    timeout=BACKUP_TIMEOUT_SECONDS,
    max_containers=1,
    volumes=BACKUP_VOLUMES,
)
def manual_backup():
    print("手动备份任务启动", flush=True)
    run_backup()


# ====================================================
# 8. 挂载调试入口
# ====================================================
# 运行：modal run rclone_modal.py::debug_ls
# 用于确认容器里每个业务 Volume 实际能看到哪些文件

@app.function(volumes=BACKUP_VOLUMES)
def debug_ls():
    for volume_name in SOURCE_VOLUMES:
        path = f"{SOURCE_ROOT}/{volume_name}"
        print("--------------------------------------", flush=True)
        print(f"{path}  isdir={os.path.isdir(path)}", flush=True)

        if os.path.isdir(path):
            entries = list_source_dir(path)
            if not entries:
                print("  （空目录）", flush=True)
            for line in entries:
                print(f"  {line}", flush=True)

    print("--------------------------------------", flush=True)
    print("rclone 视角：", flush=True)
    for volume_name in SOURCE_VOLUMES:
        path = f"{SOURCE_ROOT}/{volume_name}"
        subprocess.run(["rclone", "lsl", path], env=build_env(), check=False)
