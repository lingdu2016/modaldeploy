
import os
import time
import socket
import signal
import subprocess
import threading
import atexit
import urllib.request
import urllib.error

import modal


# ============================================================
#                    配置区（集中管理）
# ============================================================

# ---------- Modal 应用 ----------
APP_NAME = "chromium"

# ---------- Modal Volume ----------
# 保存 Chromium 完整用户配置：
# 书签、Cookie、历史记录、扩展、登录状态等。
VOLUME_NAME = "chromium-data"

# ---------- LinuxServer 官方镜像 ----------
IMAGE = "lscr.io/linuxserver/chromium:latest"

# ---------- Web 服务 ----------
# 3000：HTTP 图形界面，由 Modal HTTPS 代理转发。
WEB_PORT = 3000

WEB_STARTUP_TIMEOUT_SECONDS = 300

# ---------- Modal 资源 ----------
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 16

CPU = 1
MEMORY_MB = 1024
EPHEMERAL_DISK_MB = 8192

TIMEOUT_SECONDS = 86400
SCALEDOWN_WINDOW_SECONDS = 180

# ---------- 容器目录 ----------
CONFIG_DIR = "/config"

# ---------- 官方容器入口 ----------
INIT_PATH = "/init"

# ---------- 用户配置 ----------
PUID = "1000"
PGID = "1000"

TIMEZONE = "Asia/Shanghai"
LANGUAGE = "zh_CN.UTF-8"

# ---------- Chromium 启动参数 ----------
# Modal 不提供 Docker 的 --shm-size 参数。
# 使用 --disable-dev-shm-usage，让 Chromium 将共享内存
# 临时文件转移到磁盘，降低默认 /dev/shm 太小造成的崩溃。
CHROME_CLI = (
    "--disable-dev-shm-usage "
    "--no-first-run "
    "--no-default-browser-check"
)

# ---------- Volume 定期提交 ----------
COMMIT_INTERVAL_SECONDS = 120


# ============================================================
#                    创建 Modal Volume
# ============================================================

# 一个 Volume 保存完整浏览器用户目录。
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
#                    加载官方 Docker 镜像
# ============================================================

# LinuxServer Chromium 镜像本身不负责提供 Modal Python 运行环境。
# add_python 为 Modal Function 增加 Python 3.12。
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.12",
)

# 包装原有 ENTRYPOINT。
#
# 目的：
# Modal 启动 Python Function 时，不直接执行 LinuxServer 的 /init。
# 后续在 ui() 中显式启动官方 /init。
#
# 不修改 Chromium、Selkies 或 LinuxServer 的原始程序文件。
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


# ============================================================
#                    创建 Modal App
# ============================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
#                    运行状态
# ============================================================

init_process = None

stop_event = threading.Event()

commit_thread = None

shutdown_lock = threading.Lock()

shutdown_started = False


# ============================================================
#                    Volume 提交
# ============================================================

def commit_volume():
    """提交 Chromium 用户数据到 Modal Volume。"""

    try:
        vol.commit()

        print(
            f"[LOG] {VOLUME_NAME} 数据提交成功",
            flush=True,
        )

    except Exception as error:

        print(
            f"[ERROR] Volume 提交失败：{error}",
            flush=True,
        )


def commit_loop():
    """定期提交浏览器数据。"""

    print(
        "[LOG] Volume 定期提交线程已启动",
        flush=True,
    )

    while not stop_event.wait(
        COMMIT_INTERVAL_SECONDS
    ):

        commit_volume()


# ============================================================
#                    浏览器健康检查
# ============================================================

def wait_for_browser(process):
    """等待 LinuxServer Chromium 图形界面启动。"""

    print(
        "[LOG] 正在等待 Chromium 图形界面...",
        flush=True,
    )

    deadline = (
        time.monotonic()
        + WEB_STARTUP_TIMEOUT_SECONDS
    )

    while time.monotonic() < deadline:

        # 检查官方初始化进程是否异常退出。
        exit_code = process.poll()

        if exit_code is not None:

            raise RuntimeError(
                "LinuxServer /init 提前退出，"
                f"退出代码：{exit_code}"
            )

        # 检查官方 HTTP 图形界面端口。
        try:

            with socket.create_connection(
                ("127.0.0.1", WEB_PORT),
                timeout=3,
            ):

                print(
                    f"[LOG] Chromium 端口 {WEB_PORT} 已就绪",
                    flush=True,
                )

                return

        except OSError:
            time.sleep(2)

    raise TimeoutError(
        "等待 Chromium 图形界面启动超时。"
        "请检查 Modal 容器日志。"
    )


# ============================================================
#                    优雅关闭
# ============================================================

def shutdown():
    """
    容器正常关闭时：

    1. 停止定期提交线程。
    2. 关闭 LinuxServer 官方初始化进程。
    3. 等待浏览器进程退出。
    4. 最后提交 Modal Volume。
    """

    global shutdown_started

    with shutdown_lock:

        if shutdown_started:
            return

        shutdown_started = True

    print(
        "[LOG] 正在关闭 Chromium...",
        flush=True,
    )

    # 停止定期提交。
    stop_event.set()

    # 等待提交线程退出。
    if (
        commit_thread is not None
        and commit_thread.is_alive()
        and threading.current_thread() is not commit_thread
    ):

        commit_thread.join(timeout=15)

    # 关闭官方 /init 进程组。
    if (
        init_process is not None
        and init_process.poll() is None
    ):

        try:

            os.killpg(
                init_process.pid,
                signal.SIGTERM,
            )

            init_process.wait(timeout=30)

        except subprocess.TimeoutExpired:

            print(
                "[WARN] 浏览器关闭超时，强制终止进程组",
                flush=True,
            )

            try:

                os.killpg(
                    init_process.pid,
                    signal.SIGKILL,
                )

            except ProcessLookupError:
                pass

            init_process.wait(timeout=10)

        except ProcessLookupError:
            pass

        except Exception as error:

            print(
                f"[WARN] 关闭浏览器进程时发生异常：{error}",
                flush=True,
            )

    # 浏览器退出后再提交数据。
    commit_volume()

    print(
        "[LOG] Chromium 关闭流程完成",
        flush=True,
    )


# ============================================================
#                    启动 Chromium
# ============================================================

@app.function(
    max_containers=MAX_CONTAINERS,

    # 计算资源。
    cpu=CPU,
    memory=MEMORY_MB,
    ephemeral_disk=EPHEMERAL_DISK_MB,

    # 持久化目录。
    volumes={
        CONFIG_DIR: vol,
    },

    # 浏览器环境变量。
    env={
        "PUID": PUID,
        "PGID": PGID,

        "TZ": TIMEZONE,
        "LC_ALL": LANGUAGE,

        "CHROME_CLI": CHROME_CLI,

        # 浏览器输出使用无缓冲模式。
        "PYTHONUNBUFFERED": "1",
    },

    # 最长运行时间。
    timeout=TIMEOUT_SECONDS,

    # 空闲后缩容。
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)

# 一个容器允许处理多个 WebSocket / HTTP 输入。
# 同时只允许一个浏览器容器运行，避免用户配置冲突。
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS
)

# Modal HTTPS Web 服务。
# 开启 Modal 代理认证，避免直接公开无认证的浏览器桌面。
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    requires_proxy_auth=True,
)
def ui():

    global init_process
    global commit_thread

    print("=" * 60, flush=True)

    print(
        "[LOG] 正在启动 LinuxServer Chromium...",
        flush=True,
    )

    print("=" * 60, flush=True)

    # ========================================================
    # 1. 检查官方文件
    # ========================================================

    if not os.path.isfile(INIT_PATH):

        raise FileNotFoundError(
            f"找不到 LinuxServer 官方入口：{INIT_PATH}"
        )

    print(
        "[LOG] 官方 /init 文件检查通过",
        flush=True,
    )

    # ========================================================
    # 2. 检查持久化目录
    # ========================================================

    os.makedirs(
        CONFIG_DIR,
        exist_ok=True,
    )

    print(
        f"[LOG] 浏览器数据目录：{CONFIG_DIR}",
        flush=True,
    )

    # ========================================================
    # 3. 注册退出处理
    # ========================================================

    atexit.register(shutdown)

    # 处理 Modal 容器正常终止信号。
    signal.signal(
        signal.SIGTERM,
        lambda signum, frame: shutdown(),
    )

    signal.signal(
        signal.SIGINT,
        lambda signum, frame: shutdown(),
    )

    # ========================================================
    # 4. 启动 LinuxServer 官方 ENTRYPOINT
    # ========================================================

    print(
        "[LOG] 正在启动 LinuxServer 官方 /init...",
        flush=True,
    )

    # 单独创建进程组，方便关闭整个 s6 服务进程树。
    init_process = subprocess.Popen(
        [INIT_PATH],

        stdout=None,
        stderr=None,

        start_new_session=True,
    )

    print(
        f"[LOG] 官方 /init 已启动，PID：{init_process.pid}",
        flush=True,
    )

    # ========================================================
    # 5. 等待 HTTP 图形界面
    # ========================================================

    try:

        wait_for_browser(init_process)

    except Exception:

        # 启动失败时关闭浏览器并提交数据。
        shutdown()

        raise

    # ========================================================
    # 6. 启动 Volume 定期提交
    # ========================================================

    stop_event.clear()

    commit_thread = threading.Thread(
        target=commit_loop,
        name="chromium-volume-commit",
        daemon=True,
    )

    commit_thread.start()

    # ========================================================
    # 7. 启动完成
    # ========================================================

    print("=" * 60, flush=True)

    print(
        "[LOG] Chromium 云端浏览器启动成功",
        flush=True,
    )

    print(
        f"[LOG] 浏览器端口：{WEB_PORT}",
        flush=True,
    )

    print(
        f"[LOG] 持久化目录：{CONFIG_DIR}",
        flush=True,
    )

    print(
        f"[LOG] Volume：{VOLUME_NAME}",
        flush=True,
    )

    print(
        f"[LOG] CPU：{CPU}",
        flush=True,
    )

    print(
        f"[LOG] 内存：{MEMORY_MB} MB",
        flush=True,
    )

    print("=" * 60, flush=True)
