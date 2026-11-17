"""
Modal 上的 rclone：Web GUI + 每日自动备份 Volume 到网盘

部署：    modal deploy cpu-rclone.py
手动备份：modal run cpu-rclone.py::manual_backup

以后增加备份数据，只需要改两个地方（文件里用 ★ 标出）：
  ★ 一、目的地：新增网盘时才需要加
  ★ 二、备份项目：每新增一个要备份的数据，加一项
其余部分不用动。
"""

import os
import subprocess
import time
import socket
import threading
import modal

# ==============================================================================
# 一、目的地（备份存到哪里）
# ==============================================================================
# 目的地 = rclone 的 remote 名 + 冒号 + 路径
# remote 名必须和 Web GUI 里创建的名字完全一致。
#
# ★ 新增网盘时，在这里加一行，例如：
#   DEST_GDRIVE = "gdrive:Backups"
# 然后在下面的备份项目里用 f"{DEST_GDRIVE}/文件夹名"。

# Koofr：备份数据库、密码库等
DEST_KOOFR = "koofr:Modal-Backups"

# Tigris：备份音乐（back-music 是桶名）
DEST_TIGRIS = "tushu-tigris:back-music"


# ==============================================================================
# 二、备份项目（备份什么，备份到哪个目的地）
# ==============================================================================
# ★ 每新增一个要备份的数据，在 BACKUP_JOBS 里加一项，改完重新部署。
#
# 每一项的字段：
#
#   "volume"           Modal Volume 的真实名称（要和 Modal 控制台里完全一致）
#   "path"             要备份的内容，三种写法：
#                        ""                 整个 Volume
#                        "downloads"        Volume 里的某个文件夹
#                        "chatgpt2api.db"   Volume 里的某一个文件
#   "dest"             备份到哪里（上面的目的地 + 文件夹名）
#                      备份单个文件时，文件会放进这个文件夹里
#
#   以下是可选字段，不需要就不写：
#
#   "flatten_subdirs"  True = 把 path 下每个子文件夹"里面的内容"直接备份到 dest，
#                      不保留子文件夹这一层名字。
#                      例：downloads/Netease/周杰伦/x.mp3 -> dest/周杰伦/x.mp3
#                      path 下以后新增的子文件夹会自动包含。
#   "ignore_existing"  True = 目的地已有同名文件就跳过，不覆盖。
#                      适合不会修改的文件（如音乐）。
#                      数据库这类每天会变的文件不要开，否则备份永远是旧的。
#   "include"          只备份这些类型的文件，如 ["*.mp3", "*.lrc"]，不区分大小写。
#                      不写 = 备份所有类型。只对文件夹有效。
#   "no_traverse"      True = 不列出目的地文件，而是对源端每个文件单独问一次
#                      "目的地有没有"。源端文件很少、目的地文件很多时才开。

BACKUP_JOBS = [
    # slash-data（整个 Volume）-> Koofr
    {
        "volume": "slash-data",
        "path": "",
        "dest": f"{DEST_KOOFR}/slash-data",
    },

    # vaultwarden-data（整个 Volume）-> Koofr
    {
        "volume": "vaultwarden-data",
        "path": "",
        "dest": f"{DEST_KOOFR}/vaultwarden-data",
    },

    # chatgpt2api-data 里的单个文件 chatgpt2api.db -> Koofr
    {
        "volume": "chatgpt2api-data",
        "path": "chatgpt2api.db",
        "dest": f"{DEST_KOOFR}/chatgpt2api-data",
    },

    # 音乐：soundtrack-data/downloads 下所有子文件夹（Netease、QQ、以后新增的）
    # 里面的歌曲、歌词、封面、元数据，合并备份到 Tigris，不带 Netease / QQ 这层名字。
    # 一首歌的 4 个文件：
    #   歌名.mp3 / 歌名.lrc / 歌名.mp3.soundtrack.cover.jpg / 歌名.mp3.soundtrack.json
    {
        "volume": "soundtrack-data",
        "path": "downloads",
        "dest": DEST_TIGRIS,
        "flatten_subdirs": True,
        "ignore_existing": True,
        "include": [
            "*.mp3",
            "*.lrc",
            "*.soundtrack.cover.*",
            "*.soundtrack.json",
        ],
        "no_traverse": False,
    },
]


# ==============================================================================
# 以下是程序逻辑和运行参数，以后增加备份数据时不需要修改
# ==============================================================================

APP_NAME = "rclone"

# 保存 rclone.conf 的 Volume
VOLUME_NAME = "rclone-data"

#################################################################
# 需要挂载的业务 Volume（自动从 BACKUP_JOBS 生成）
SOURCE_VOLUMES = sorted({job["volume"] for job in BACKUP_JOBS})

# rclone 官方镜像版本（锁定 1.75.0，保留旧版 Web GUI）
IMAGE = "rclone/rclone:1.75.0"

# ------------------------------ Web GUI --------------------------------------

WEB_PORT = 5572
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8
TIMEOUT_SECONDS = 3600

# 无人访问多久后容器关闭（秒）
SCALEDOWN_WINDOW_SECONDS = 90

# 启动等待时间（秒）。首次启动要下载 Web GUI 前端，所以给得比较长
WEB_STARTUP_TIMEOUT_SECONDS = 180

# Web 容器每隔多少秒刷新一次业务 Volume
# 长期运行的容器看到的是启动时的快照，定期 reload 才能看到新文件
WEB_RELOAD_INTERVAL_SECONDS = 60

CPU = 0.25
MEMORY_MB = 128

# ------------------------------ 备份任务 -------------------------------------

BACKUP_CPU = 0.25
BACKUP_MEMORY_MB = 512

# 备份任务最长运行时间（秒），3600 = 1 小时
BACKUP_TIMEOUT_SECONDS = 3600

# 失败自动重试。注意：Modal 要求重试延迟必须在 0-60 秒之间，超过会部署失败
BACKUP_MAX_RETRIES = 3
BACKUP_RETRY_DELAY_SECONDS = 60.0

# 每天自动备份的时间。cron 格式：分 时 日 月 星期
BACKUP_CRON = "31 1 * * *"
BACKUP_TIMEZONE = "Asia/Shanghai"

# 并发传输数量
BACKUP_TRANSFERS = 2
BACKUP_CHECKERS = 4

# ------------------------------ 路径与密钥 -----------------------------------

# Volume 在容器里的挂载位置，如 slash-data -> /mnt/sources/slash-data
SOURCE_ROOT = "/mnt/sources"

# Modal Secret 名称，里面必须有：
#   RCLONE_WEB_USER   Web GUI 登录用户名
#   RCLONE_WEB_PASS   Web GUI 登录密码（至少 12 个字符）
SECRET_NAME = "rclone-secret"

# rclone 配置和缓存目录
CONFIG_DIR = "/config"
CACHE_DIR = "/cache"
CONFIG_FILE = "/config/rclone.conf"


# ------------------------------------------------------------------------------
# 1. 引用 Modal Volumes
# ------------------------------------------------------------------------------

# rclone 配置 Volume（已存在，不自动创建）
config_volume = modal.Volume.from_name(VOLUME_NAME)

# 业务 Volume：只读挂载，rclone 无法修改源数据
source_volume_objects = {
    volume_name: modal.Volume.from_name(volume_name).with_mount_options(
        read_only=True
    )
    for volume_name in SOURCE_VOLUMES
}

# 例如 {"/mnt/sources/slash-data": Volume, ...}
SOURCE_MOUNT_PATHS = {
    f"{SOURCE_ROOT}/{volume_name}": volume
    for volume_name, volume in source_volume_objects.items()
}

# Web GUI 用：配置可读写（需要保存网盘配置），业务数据只读
WEB_VOLUMES = {
    CONFIG_DIR: config_volume,
    **SOURCE_MOUNT_PATHS,
}

# 备份任务用：配置也只读，防止备份任务改动配置
BACKUP_VOLUMES = {
    CONFIG_DIR: config_volume.with_mount_options(read_only=True),
    **SOURCE_MOUNT_PATHS,
}


# ------------------------------------------------------------------------------
# 2. 镜像：rclone 官方镜像 + Python
# ------------------------------------------------------------------------------

# 官方镜像基于 Alpine，不带 Python，而 Modal 函数需要 Python
image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 官方镜像的 ENTRYPOINT 是 rclone，会把 Modal 的启动参数当成 rclone 参数。
# 这里改成 shell 透传，让传入的命令原样执行。
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


# ------------------------------------------------------------------------------
# 3. 创建 Modal App
# ------------------------------------------------------------------------------

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ------------------------------------------------------------------------------
# 4. 公共工具函数
# ------------------------------------------------------------------------------

def build_env():
    """构建 rclone 子进程的环境变量（时区 + 配置文件路径）。"""
    env = os.environ.copy()
    env["TZ"] = BACKUP_TIMEZONE
    env["RCLONE_CONFIG"] = CONFIG_FILE
    return env


def remote_of(destination):
    """取出目的地里的 remote 名，例如 'koofr:Modal-Backups/x' -> 'koofr:'。"""
    return destination.split(":", 1)[0] + ":"


def list_source_dir(path):
    """列出目录内容（类型、大小、名称），用于日志。"""
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
    """按 BACKUP_JOBS 逐个执行备份（rclone copy）。"""
    print("======================================", flush=True)
    print("开始执行 Modal 备份", flush=True)
    print("======================================", flush=True)

    # 4.1 检查 rclone 配置文件是否存在
    if not os.path.isfile(CONFIG_FILE):
        raise FileNotFoundError(f"找不到 rclone 配置文件：{CONFIG_FILE}")

    env = build_env()

    # 4.2 检查所有用到的目的地 remote 是否已配置
    result = subprocess.run(
        ["rclone", "listremotes"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    existing_remotes = set(result.stdout.splitlines())
    needed_remotes = {remote_of(job["dest"]) for job in BACKUP_JOBS}
    missing = needed_remotes - existing_remotes

    if missing:
        raise RuntimeError(
            f"rclone.conf 里没有这些 Remote：{sorted(missing)}，"
            "请先在 Web GUI 里配置。"
        )

    print(f"Remote 检查成功：{sorted(needed_remotes)}", flush=True)

    # 4.3 逐个项目备份
    errors = []

    for job in BACKUP_JOBS:
        volume_name = job["volume"]
        sub_path = job.get("path", "").strip("/")
        source_path = f"{SOURCE_ROOT}/{volume_name}"
        if sub_path:
            source_path = f"{source_path}/{sub_path}"

        destination_path = job["dest"]
        label = f"{volume_name}/{sub_path}" if sub_path else volume_name

        print("--------------------------------------", flush=True)
        print(f"备份项目：{label}", flush=True)
        print(f"源路径：{source_path}", flush=True)
        print(f"目的地：{destination_path}", flush=True)

        is_file = os.path.isfile(source_path)

        # 源路径既不是文件也不是目录：Volume 没挂载上，或路径写错
        if not is_file and not os.path.isdir(source_path):
            message = f"源路径不存在或未挂载：{source_path}"
            print(message, flush=True)
            errors.append(message)
            continue

        if is_file:
            # 单个文件：打印大小
            size = os.path.getsize(source_path)
            print(f"源是单个文件，大小 {size} 字节", flush=True)

        else:
            # 目录：打印内容，方便在日志里确认读到了哪些文件
            entries = list_source_dir(source_path)
            print(f"源目录当前内容（{len(entries)} 项）：", flush=True)
            for line in entries[:50]:
                print(f"  {line}", flush=True)
            if len(entries) > 50:
                print(f"  ...（其余 {len(entries) - 50} 项省略）", flush=True)

            # 空目录按错误处理，避免"空备份也显示成功"
            if not entries:
                message = (
                    f"警告：{source_path} 是空目录，"
                    "请确认 Volume 名称、路径和 Modal 环境是否正确"
                )
                print(message, flush=True)
                errors.append(message)
                continue

        # 本项目的额外参数
        job_extra = []

        if job.get("ignore_existing"):
            job_extra.append("--ignore-existing")

        if job.get("no_traverse"):
            job_extra.append("--no-traverse")

        # 文件类型过滤：只备份指定扩展名，且不区分大小写（只对目录有效）
        includes = [] if is_file else (job.get("include") or [])
        for pattern in includes:
            job_extra += ["--include", pattern]
        if includes:
            job_extra.append("--ignore-case")

        # 有过滤时不创建空目录，避免目的地出现没有内容的空文件夹
        dir_flags = [] if includes else ["--create-empty-src-dirs"]

        # 拆成若干个 rclone copy 步骤：(源, 目的地, 额外参数)
        if is_file:
            # 单个文件：直接复制到目的地文件夹里
            steps = [(source_path, destination_path, [])]

        elif job.get("flatten_subdirs"):
            steps = []

            # 每个子文件夹：把"里面的内容"复制到目的地，不带子文件夹名
            for name in sorted(os.listdir(source_path)):
                if os.path.isdir(os.path.join(source_path, name)):
                    steps.append((f"{source_path}/{name}", destination_path, []))

            # source_path 顶层直接放着的散文件（不进子文件夹）
            steps.append((source_path, destination_path, ["--max-depth", "1"]))

        else:
            steps = [(source_path, destination_path, [])]

        # rclone copy：只复制，不会删除目的地已有的文件
        # 已存在且没变化的文件会自动跳过（增量）；变化了的文件会覆盖旧版本
        for src, dst, step_extra in steps:
            print(f"执行：rclone copy {src} -> {dst}", flush=True)

            command = [
                "rclone",
                "copy",
                src,
                dst,
                *dir_flags,
                "--transfers", str(BACKUP_TRANSFERS),
                "--checkers", str(BACKUP_CHECKERS),
                "--retries", "3",
                "--low-level-retries", "10",
                "--stats", "30s",
                "--stats-one-line",
                "--log-level", "INFO",
                *job_extra,
                *step_extra,
            ]

            try:
                subprocess.run(command, env=env, check=True)

            except subprocess.CalledProcessError as e:
                message = f"{src} 备份失败，退出代码：{e.returncode}"
                print(message, flush=True)
                errors.append(message)

        print(f"{label} 处理完成。", flush=True)

    # 4.4 汇总：只要有一个失败，整个任务就标记为失败（Modal 会触发重试）
    print("======================================", flush=True)

    if errors:
        print("备份存在错误：", flush=True)
        for error in errors:
            print(error, flush=True)
        raise RuntimeError(f"备份完成，但有 {len(errors)} 个问题。")

    print("所有备份项目均已完成。", flush=True)
    print("======================================", flush=True)


def reload_worker():
    """
    后台线程：定期 reload 业务 Volume。
    Web GUI 容器长期运行，挂载内容是启动时的快照，
    reload 之后才能看到其他容器之后提交的新文件。
    """
    while True:
        time.sleep(WEB_RELOAD_INTERVAL_SECONDS)

        for volume_name, volume in source_volume_objects.items():
            try:
                volume.reload()
            except Exception as e:
                print(f"Volume reload 失败（{volume_name}）：{e}", flush=True)


# ------------------------------------------------------------------------------
# 5. Rclone Web GUI 服务（唯一有公网链接的函数）
# ------------------------------------------------------------------------------
# 用途：配置网盘 remote（只需做一次）、浏览文件。

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

    # 5.1 读取并检查登录账号密码（来自 Modal Secret）
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
    #     账号密码通过环境变量传给 rclone，不出现在进程命令行里
    env = build_env()
    env["RCLONE_RC_USER"] = web_user
    env["RCLONE_RC_PASS"] = web_pass

    # 5.4 启动前打印业务 Volume 内容，便于排查挂载问题
    for volume_name in SOURCE_VOLUMES:
        path = f"{SOURCE_ROOT}/{volume_name}"
        if os.path.isdir(path):
            print(f"{path} 内容：{os.listdir(path)}", flush=True)
        else:
            print(f"{path} 不存在，Volume 可能未挂载。", flush=True)

    # 5.5 后台定期刷新业务 Volume
    threading.Thread(target=reload_worker, daemon=True).start()

    # 5.6 启动 rclone Web GUI
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

    # 5.7 等待端口就绪（Modal 要求这个函数在端口可用后才返回）
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


# ------------------------------------------------------------------------------
# 6. 每日自动备份（定时触发，没有公网链接）
# ------------------------------------------------------------------------------

@app.function(
    schedule=modal.Cron(BACKUP_CRON, timezone=BACKUP_TIMEZONE),
    retries=modal.Retries(
        max_retries=BACKUP_MAX_RETRIES,
        initial_delay=BACKUP_RETRY_DELAY_SECONDS,
        backoff_coefficient=1.0,  # 每次重试间隔固定
        max_delay=60.0,
    ),
    cpu=BACKUP_CPU,
    memory=BACKUP_MEMORY_MB,
    timeout=BACKUP_TIMEOUT_SECONDS,
    max_containers=1,
    volumes=BACKUP_VOLUMES,
)
def daily_backup():
    print("Modal 定时备份任务启动", flush=True)
    run_backup()


# ------------------------------------------------------------------------------
# 7. 手动备份入口（需要时手动调用，不影响定时任务）
# ------------------------------------------------------------------------------
# 运行：modal run cpu-rclone.py::manual_backup

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
