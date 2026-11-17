
import os
import subprocess
import time
import socket
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "rclone"

# ---------- Modal Volume 配置 ----------

# rclone 自己的配置 Volume
# 保存 rclone.conf，不参与业务数据备份
VOLUME_NAME = "rclone-data"

# 需要备份的 Modal Volume
#
# 左侧：Modal Volume 的真实名称
# 右侧：Koofr 中对应的备份目录名称
#
# 后续增加项目，只需要在这里增加一行即可。
SOURCE_VOLUMES = {
    "qwen2api-data": "qwen2api-data",
    "slash-data": "slash-data",
}

# ---------- 官方镜像 ----------

# 锁定 rclone 1.75.0
# 保留旧版 Web GUI
IMAGE = "rclone/rclone:1.75.0"

# ---------- Web 服务配置 ----------

WEB_PORT = 5572

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 180

# ---------- Web 服务资源 ----------

CPU = 0.25
MEMORY_MB = 256

# ---------- 自动备份资源 ----------

BACKUP_CPU = 0.25
BACKUP_MEMORY_MB = 512

# 备份任务最长运行时间：6 小时
BACKUP_TIMEOUT_SECONDS = 21600

# ---------- 自动备份时间 ----------

# 每天北京时间下午 13:30
BACKUP_CRON = "30 13 * * *"
BACKUP_TIMEZONE = "Asia/Shanghai"

# ---------- Koofr 备份配置 ----------

# Koofr 中的备份根目录
BACKUP_ROOT = "koofr:Modal-Backups"

# ---------- 本地 Volume 挂载路径 ----------

SOURCE_ROOT = "/mnt/sources"

# ---------- Modal Secret ----------

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

# rclone 配置 Volume
# 已存在的 Volume，不自动创建新 Volume
config_volume = modal.Volume.from_name(
    VOLUME_NAME,
)

# 引用所有需要备份的业务 Volume
#
# 每个业务 Volume 设置为只读挂载
# rclone 只能读取文件，不能修改源数据
source_volume_objects = {
    volume_name: modal.Volume.from_name(
        volume_name
    ).with_mount_options(
        read_only=True
    )
    for volume_name in SOURCE_VOLUMES
}

# 自动生成 Volume 挂载路径
#
# 例如：
# qwen2api-data -> /mnt/sources/qwen2api-data
# slash-data    -> /mnt/sources/slash-data

SOURCE_MOUNT_PATHS = {
    f"{SOURCE_ROOT}/{volume_name}": volume
    for volume_name, volume in source_volume_objects.items()
}

# Web Explorer 使用的 Volume
#
# /config 可读写，用于保存 rclone.conf
# 业务 Volume 只读，用于浏览和检查文件

WEB_VOLUMES = {
    CONFIG_DIR: config_volume,
    **SOURCE_MOUNT_PATHS,
}

# 自动备份函数使用的 Volume
#
# 配置 Volume 只读
# 业务 Volume 只读

BACKUP_VOLUMES = {
    CONFIG_DIR: config_volume.with_mount_options(
        read_only=True
    ),
    **SOURCE_MOUNT_PATHS,
}


# ====================================================
# 2. 官方镜像 + 安装 Python
# ====================================================

# 官方 rclone 镜像基于 Alpine
# 安装 Python 3，供 Modal Python Function 使用

image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 官方镜像原本 ENTRYPOINT = ["rclone"]
# 使用 shell 转交参数，避免 rclone 错误接收 Modal 启动参数

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
# 4. 通用备份函数
# ====================================================

def run_backup():

    print("======================================", flush=True)
    print("开始执行 Modal → Koofr 备份", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 4.1 检查 rclone 配置文件
    # ------------------------------------------------

    if not os.path.isfile(CONFIG_FILE):
        raise FileNotFoundError(
            f"找不到 rclone 配置文件：{CONFIG_FILE}"
        )

    # ------------------------------------------------
    # 4.2 设置环境变量
    # ------------------------------------------------

    env = os.environ.copy()

    env["TZ"] = BACKUP_TIMEZONE
    env["RCLONE_CONFIG"] = CONFIG_FILE
    env["XDG_CONFIG_HOME"] = CONFIG_DIR

    # ------------------------------------------------
    # 4.3 检查 Koofr Remote
    # ------------------------------------------------

    result = subprocess.run(
        [
            "rclone",
            "listremotes",
            "--config",
            CONFIG_FILE,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    remotes = result.stdout.splitlines()

    if "koofr:" not in remotes:
        raise RuntimeError(
            "没有找到 koofr: Remote，请检查 rclone.conf。"
        )

    print("Koofr 配置检查成功。", flush=True)

    # ------------------------------------------------
    # 4.4 逐个备份业务 Volume
    # ------------------------------------------------

    errors = []

    for volume_name, backup_folder in SOURCE_VOLUMES.items():

        source_path = (
            f"{SOURCE_ROOT}/{volume_name}"
        )

        destination_path = (
            f"{BACKUP_ROOT}/{backup_folder}"
        )

        print("--------------------------------------", flush=True)
        print(f"备份项目：{volume_name}", flush=True)
        print(f"源目录：{source_path}", flush=True)
        print(f"目标目录：{destination_path}", flush=True)

        # 检查 Volume 是否成功挂载
        if not os.path.isdir(source_path):

            message = (
                f"源目录不存在或未挂载：{source_path}"
            )

            print(message, flush=True)
            errors.append(message)
            continue

        # ------------------------------------------------
        # 4.5 构建 rclone copy 命令
        # ------------------------------------------------

        command = [
            "rclone",
            "copy",

            source_path,
            destination_path,

            "--config",
            CONFIG_FILE,

            # 保留空目录
            "--create-empty-src-dirs",

            # 并发传输数量
            "--transfers",
            str(BACKUP_TRANSFERS),

            # 并发检查数量
            "--checkers",
            str(BACKUP_CHECKERS),

            # 重试次数
            "--retries",
            "3",

            # 底层重试次数
            "--low-level-retries",
            "10",

            # 每 30 秒输出一次传输统计
            "--stats",
            "30s",

            "--stats-one-line",

            # 日志等级
            "--log-level",
            "INFO",
        ]

        print(
            f"开始执行：rclone copy {source_path} {destination_path}",
            flush=True,
        )

        # ------------------------------------------------
        # 4.6 执行备份
        # ------------------------------------------------

        try:

            subprocess.run(
                command,
                env=env,
                check=True,
            )

            print(
                f"{volume_name} 备份成功。",
                flush=True,
            )

        except subprocess.CalledProcessError as e:

            message = (
                f"{volume_name} 备份失败，退出代码：{e.returncode}"
            )

            print(message, flush=True)
            errors.append(message)

    # ------------------------------------------------
    # 4.7 备份结果
    # ------------------------------------------------

    print("======================================", flush=True)

    if errors:

        print("自动备份存在错误：", flush=True)

        for error in errors:
            print(error, flush=True)

        raise RuntimeError(
            f"备份完成，但有 {len(errors)} 个任务失败。"
        )

    print("所有业务 Volume 均已成功备份到 Koofr。", flush=True)
    print("======================================", flush=True)


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

    print("======================================", flush=True)
    print("正在启动 Rclone...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 5.1 检查 Python
    # ------------------------------------------------

    import sys

    print(f"Python 版本：{sys.version}", flush=True)

    # ------------------------------------------------
    # 5.2 检查 Rclone
    # ------------------------------------------------

    result = subprocess.run(
        ["rclone", "version"],
        capture_output=True,
        text=True,
        check=True,
    )

    print(result.stdout, flush=True)

    # ------------------------------------------------
    # 5.3 检查 Web 身份验证 Secret
    # ------------------------------------------------

    web_user = os.environ.get(
        "RCLONE_WEB_USER", ""
    ).strip()

    web_pass = os.environ.get(
        "RCLONE_WEB_PASS", ""
    ).strip()

    if not web_user:
        raise ValueError(
            "RCLONE_WEB_USER 未设置，请检查 Modal Secret。"
        )

    if not web_pass:
        raise ValueError(
            "RCLONE_WEB_PASS 未设置，请检查 Modal Secret。"
        )

    if len(web_pass) < 12:
        raise ValueError(
            "RCLONE_WEB_PASS 不能少于 12 个字符。"
        )

    # ------------------------------------------------
    # 5.4 创建目录
    # ------------------------------------------------

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # ------------------------------------------------
    # 5.5 设置环境变量
    # ------------------------------------------------

    env = os.environ.copy()

    env["TZ"] = BACKUP_TIMEZONE
    env["RCLONE_CONFIG"] = CONFIG_FILE
    env["XDG_CONFIG_HOME"] = CONFIG_DIR

    # ------------------------------------------------
    # 5.6 Rclone Web GUI 启动命令
    # ------------------------------------------------

    command = [
        "rclone",
        "rcd",

        "--rc-addr",
        f"0.0.0.0:{WEB_PORT}",

        "--rc-user",
        web_user,

        "--rc-pass",
        web_pass,

        "--rc-web-gui",
        "--rc-web-gui-no-open-browser",

        "--config",
        CONFIG_FILE,

        "--cache-dir",
        CACHE_DIR,

        "--log-level",
        "INFO",
    ]

    # ------------------------------------------------
    # 5.7 输出启动信息
    # ------------------------------------------------

    print("======================================", flush=True)

    print(f"官方镜像：{IMAGE}", flush=True)
    print(f"Web 端口：{WEB_PORT}", flush=True)

    print("身份验证：已启用", flush=True)

    print(f"配置目录：{CONFIG_DIR}", flush=True)
    print(f"配置文件：{CONFIG_FILE}", flush=True)
    print(f"缓存目录：{CACHE_DIR}", flush=True)

    print("已挂载的业务 Volume：", flush=True)

    for volume_name in SOURCE_VOLUMES:

        print(
            f"{volume_name} -> {SOURCE_ROOT}/{volume_name}",
            flush=True,
        )

    print(f"CPU：{CPU}", flush=True)
    print(f"内存：{MEMORY_MB} MB", flush=True)

    print("======================================", flush=True)

    # ------------------------------------------------
    # 5.8 启动服务
    # ------------------------------------------------

    process = subprocess.Popen(
        command,
        env=env,
    )

    print(
        f"Rclone 进程已启动，PID：{process.pid}",
        flush=True,
    )

    # ------------------------------------------------
    # 5.9 等待服务端口就绪
    # ------------------------------------------------

    start_time = time.monotonic()

    while (
        time.monotonic() - start_time
        < WEB_STARTUP_TIMEOUT_SECONDS
    ):

        # 检查进程是否意外退出
        exit_code = process.poll()

        if exit_code is not None:

            raise RuntimeError(
                f"Rclone 启动失败，退出代码：{exit_code}"
            )

        # 检查端口
        try:

            with socket.create_connection(
                ("127.0.0.1", WEB_PORT),
                timeout=1,
            ):

                print(
                    f"Rclone 已成功启动，监听端口：{WEB_PORT}",
                    flush=True,
                )

                break

        except OSError:

            time.sleep(0.5)

    else:

        raise RuntimeError(
            "Rclone 启动超时，请检查 Modal 容器日志。"
        )

    print("======================================", flush=True)
    print("Rclone Web GUI 服务已就绪。", flush=True)
    print("======================================", flush=True)


# ====================================================
# 6. 每天北京时间 13:30 自动备份
# ====================================================

@app.function(
    schedule=modal.Cron(
        BACKUP_CRON,
        timezone=BACKUP_TIMEZONE,
    ),

    cpu=BACKUP_CPU,
    memory=BACKUP_MEMORY_MB,

    timeout=BACKUP_TIMEOUT_SECONDS,

    max_containers=1,

    volumes=BACKUP_VOLUMES,
)
def daily_backup():

    print("======================================", flush=True)
    print("Modal 定时备份任务启动", flush=True)
    print("执行时间：北京时间每天 13:30", flush=True)
    print("======================================", flush=True)

    run_backup()


# ====================================================
# 7. 手动备份入口
# ====================================================

# 用于第一次部署后测试
# 也可以在需要时手动执行
# 不影响每天 13:30 的自动备份计划

@app.function(
    cpu=BACKUP_CPU,
    memory=BACKUP_MEMORY_MB,

    timeout=BACKUP_TIMEOUT_SECONDS,

    max_containers=1,

    volumes=BACKUP_VOLUMES,
)
def manual_backup():

    print("======================================", flush=True)
    print("手动备份任务启动", flush=True)
    print("======================================", flush=True)

    run_backup()
