
import os
import time
import signal
import socket
import shutil
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
# 保存 Chromium 浏览器配置、书签、Cookie 和扩展等数据。
VOLUME_NAME = "chromium-data"

# ---------- 浏览器 ----------
# 使用 Debian 官方软件包安装 Chromium。
CHROMIUM_BINARY = "/usr/bin/chromium"

# ---------- Web 服务 ----------
# noVNC 网页入口，由 Modal 对外提供 HTTPS 访问。
WEB_PORT = 3000

# VNC 内部通信端口，只监听容器本地地址。
VNC_PORT = 5900

# Xvfb 虚拟显示器。
DISPLAY_NUM = 99
DISPLAY = f":{DISPLAY_NUM}"

# 虚拟桌面分辨率。
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
SCREEN_DEPTH = 24

# ---------- Modal 资源 ----------
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 16

CPU = 1
MEMORY_MB = 1024

# 不设置 ephemeral_disk。
# 使用 Modal 默认临时磁盘额度。
# 不要填写 8192，也不需要手动申请 524288。

TIMEOUT_SECONDS = 86400
SCALEDOWN_WINDOW_SECONDS = 180

# ---------- 浏览器数据目录 ----------
CONFIG_DIR = "/config"

# ---------- 时区 ----------
TIMEZONE = "Asia/Shanghai"

# ---------- 定时提交 Volume ----------
COMMIT_INTERVAL_SECONDS = 300

# ---------- 启动超时 ----------
WEB_STARTUP_TIMEOUT_SECONDS = 300

# ---------- 浏览器启动参数 ----------
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--password-store=basic",
    "--start-maximized",
    "--ozone-platform=x11",
    "--lang=zh-CN",
    f"--user-data-dir={CONFIG_DIR}",
]


# ============================================================
#                    创建 Modal Volume
# ============================================================

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
#                    构建浏览器镜像
# ============================================================

# 不使用 LinuxServer Chromium 的 /init。
#
# 直接使用 Debian + Chromium + Xvfb + x11vnc + noVNC。
#
# 这样可以由 Modal Python Function 直接管理各个进程，
# 避免将要求 PID 1 的 s6-overlay /init 当作普通子进程启动。

image = (
    modal.Image.debian_slim(
        python_version="3.12",
    )
    .apt_install(
        "chromium",
        "xvfb",
        "x11vnc",
        "novnc",
        "websockify",
        "openbox",
        "x11-utils",
        "fonts-noto-cjk",
        "fonts-liberation",
        "dbus-x11",
    )
)


# ============================================================
#                    创建 Modal App
# ============================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
#                    全局进程管理
# ============================================================

processes = {}

stop_event = threading.Event()

commit_thread = None

shutdown_lock = threading.Lock()

shutdown_started = False


# ============================================================
#                    网络端口检查
# ============================================================

def port_is_open(host, port):
    """检查 TCP 端口是否已经监听。"""

    try:
        with socket.create_connection(
            (host, port),
            timeout=2,
        ):
            return True

    except OSError:
        return False


def wait_for_port(
    host,
    port,
    timeout=60,
    process_name=None,
):
    """等待指定 TCP 端口启动。"""

    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:

        if port_is_open(host, port):
            return True

        if process_name:
            process = processes.get(process_name)

            if process and process.poll() is not None:
                raise RuntimeError(
                    f"{process_name} 启动失败，"
                    f"退出代码：{process.returncode}"
                )

        time.sleep(1)

    raise TimeoutError(
        f"等待端口超时：{host}:{port}"
    )


# ============================================================
#                    HTTP 健康检查
# ============================================================

def http_health_check(url):
    """检查 noVNC 网页是否可以正常访问。"""

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Modal-Chromium-HealthCheck",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=5,
        ) as response:

            return response.status == 200

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ):
        return False


def wait_for_http(
    url,
    timeout=60,
):
    """等待 HTTP 服务正常响应。"""

    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:

        if http_health_check(url):
            return True

        for name, process in processes.items():

            if process.poll() is not None:
                raise RuntimeError(
                    f"{name} 进程提前退出，"
                    f"退出代码：{process.returncode}"
                )

        time.sleep(1)

    raise TimeoutError(
        f"HTTP 健康检查超时：{url}"
    )


# ============================================================
#                    启动子进程
# ============================================================

def start_process(
    name,
    command,
    env,
):
    """启动一个独立管理的子进程。"""

    print(
        f"[LOG] 正在启动 {name}...",
        flush=True,
    )

    process = subprocess.Popen(
        command,
        env=env,
        stdout=None,
        stderr=None,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    processes[name] = process

    print(
        f"[LOG] {name} 已启动，PID：{process.pid}",
        flush=True,
    )

    return process


# ============================================================
#                    等待 X11 显示器
# ============================================================

def wait_for_display(
    env,
    timeout=60,
):
    """通过 xdpyinfo 检查虚拟显示器是否正常。"""

    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:

        process = processes.get("Xvfb")

        if process and process.poll() is not None:
            raise RuntimeError(
                "Xvfb 启动失败，"
                f"退出代码：{process.returncode}"
            )

        try:
            result = subprocess.run(
                [
                    "xdpyinfo",
                    "-display",
                    DISPLAY,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )

            if result.returncode == 0:
                print(
                    "[LOG] X11 虚拟显示器启动成功",
                    flush=True,
                )
                return

        except (
            subprocess.TimeoutExpired,
            OSError,
        ):
            pass

        time.sleep(1)

    raise TimeoutError(
        "X11 虚拟显示器启动超时。"
    )


# ============================================================
#                    Volume 定时提交
# ============================================================

def commit_volume():
    """提交 Chromium 浏览器数据。"""

    try:
        vol.commit()

        print(
            f"[LOG] {VOLUME_NAME} 已成功提交",
            flush=True,
        )

    except Exception as error:

        print(
            f"[ERROR] Volume 提交失败：{error}",
            flush=True,
        )


def commit_loop():
    """定时提交浏览器数据。"""

    while not stop_event.wait(
        COMMIT_INTERVAL_SECONDS
    ):
        commit_volume()


def start_commit_thread():
    """启动 Volume 定时提交线程。"""

    global commit_thread

    commit_thread = threading.Thread(
        target=commit_loop,
        name="volume-commit",
        daemon=True,
    )

    commit_thread.start()

    print(
        "[LOG] Volume 定时提交线程已启动",
        flush=True,
    )


# ============================================================
#                    优雅关闭
# ============================================================

def stop_process(name, process):
    """终止进程及其进程组。"""

    if process.poll() is not None:
        return

    print(
        f"[LOG] 正在关闭 {name}...",
        flush=True,
    )

    try:
        os.killpg(
            process.pid,
            signal.SIGTERM,
        )

    except ProcessLookupError:
        return

    try:
        process.wait(timeout=20)

    except subprocess.TimeoutExpired:

        print(
            f"[LOG] {name} 未及时退出，执行强制终止",
            flush=True,
        )

        try:
            os.killpg(
                process.pid,
                signal.SIGKILL,
            )
        except ProcessLookupError:
            pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def shutdown():
    """关闭全部服务并提交浏览器数据。"""

    global shutdown_started

    with shutdown_lock:

        if shutdown_started:
            return

        shutdown_started = True

    print(
        "[LOG] 正在执行 Chromium 优雅关闭...",
        flush=True,
    )

    # 1. 停止定时提交线程。
    stop_event.set()

    if (
        commit_thread
        and commit_thread.is_alive()
        and threading.current_thread() != commit_thread
    ):
        commit_thread.join(timeout=10)

    # 2. 按照启动顺序的反方向关闭进程。
    shutdown_order = [
        "Websockify",
        "x11vnc",
        "Chromium",
        "Openbox",
        "Xvfb",
    ]

    for name in shutdown_order:

        process = processes.get(name)

        if process:
            stop_process(
                name,
                process,
            )

    # 3. 浏览器关闭后再进行最后一次提交。
    print(
        "[LOG] 正在执行 Volume 最终提交...",
        flush=True,
    )

    commit_volume()

    print(
        "[LOG] Chromium 已关闭",
        flush=True,
    )


# ============================================================
#                    信号处理
# ============================================================

def handle_signal(signum, frame):
    """接收 Modal 容器关闭信号。"""

    print(
        f"[LOG] 收到关闭信号：{signum}",
        flush=True,
    )

    shutdown()

    raise SystemExit(0)


# ============================================================
#                    启动 Chromium
# ============================================================

@app.function(
    max_containers=MAX_CONTAINERS,

    cpu=CPU,

    memory=MEMORY_MB,

    # 持久化浏览器配置目录。
    volumes={
        CONFIG_DIR: vol,
    },

    # 不设置 ephemeral_disk。
    # 使用 Modal 默认临时磁盘额度。

    timeout=TIMEOUT_SECONDS,

    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

    env={
        "TZ": TIMEZONE,
        "DISPLAY": DISPLAY,
        "HOME": CONFIG_DIR,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    },
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    requires_proxy_auth=True,
)
def ui():

    print("=" * 60, flush=True)
    print("[LOG] 正在启动 Modal Chromium...", flush=True)
    print("=" * 60, flush=True)

    # ========================================================
    # 1. 检查必要程序
    # ========================================================

    required_programs = [
        CHROMIUM_BINARY,
        "/usr/bin/Xvfb",
        "/usr/bin/x11vnc",
        "/usr/bin/openbox",
        "/usr/bin/websockify",
        "/usr/bin/xdpyinfo",
    ]

    for program in required_programs:

        if not os.path.isfile(program):
            raise FileNotFoundError(
                f"找不到必要程序：{program}"
            )

    if not os.path.isdir("/usr/share/novnc"):
        raise FileNotFoundError(
            "找不到 noVNC 网页目录：/usr/share/novnc"
        )

    print(
        "[LOG] 必要程序检查通过",
        flush=True,
    )

    # ========================================================
    # 2. 创建浏览器数据目录
    # ========================================================

    os.makedirs(
        CONFIG_DIR,
        mode=0o700,
        exist_ok=True,
    )

    os.makedirs(
        "/tmp/runtime-chromium",
        mode=0o700,
        exist_ok=True,
    )

    os.environ["XDG_RUNTIME_DIR"] = (
        "/tmp/runtime-chromium"
    )

    # ========================================================
    # 3. 注册退出处理
    # ========================================================

    atexit.register(shutdown)

    signal.signal(
        signal.SIGTERM,
        handle_signal,
    )

    signal.signal(
        signal.SIGINT,
        handle_signal,
    )

    # ========================================================
    # 4. 准备运行环境
    # ========================================================

    env = os.environ.copy()

    env["DISPLAY"] = DISPLAY
    env["HOME"] = CONFIG_DIR
    env["XDG_RUNTIME_DIR"] = "/tmp/runtime-chromium"
    env["TZ"] = TIMEZONE
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"

    print(
        "[LOG] 浏览器运行环境准备完成",
        flush=True,
    )

    # ========================================================
    # 5. 启动 Xvfb 虚拟显示器
    # ========================================================

    xvfb_command = [
        "/usr/bin/Xvfb",
        DISPLAY,
        "-screen",
        "0",
        f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x{SCREEN_DEPTH}",
        "-ac",
        "+extension",
        "RANDR",
        "-nolisten",
        "tcp",
        "-noreset",
    ]

    start_process(
        "Xvfb",
        xvfb_command,
        env,
    )

    wait_for_display(
        env,
        timeout=60,
    )

    # ========================================================
    # 6. 启动 Openbox 窗口管理器
    # ========================================================

    openbox_command = [
        "/usr/bin/openbox",
        "--sm-disable",
    ]

    start_process(
        "Openbox",
        openbox_command,
        env,
    )

    time.sleep(2)

    if processes["Openbox"].poll() is not None:
        raise RuntimeError(
            "Openbox 启动失败。"
        )

    # ========================================================
    # 7. 启动 Chromium 浏览器
    # ========================================================

    chromium_command = [
        CHROMIUM_BINARY,
        *CHROMIUM_ARGS,
    ]

    start_process(
        "Chromium",
        chromium_command,
        env,
    )

    time.sleep(5)

    if processes["Chromium"].poll() is not None:
        raise RuntimeError(
            "Chromium 启动失败，"
            f"退出代码：{processes['Chromium'].returncode}"
        )

    print(
        "[LOG] Chromium 浏览器启动成功",
        flush=True,
    )

    # ========================================================
    # 8. 启动 x11vnc
    # ========================================================

    x11vnc_command = [
        "/usr/bin/x11vnc",
        "-display",
        DISPLAY,
        "-forever",
        "-shared",
        "-rfbport",
        str(VNC_PORT),
        "-localhost",
        "-nopw",
        "-noxdamage",
        "-repeat",
        "-xkb",
    ]

    start_process(
        "x11vnc",
        x11vnc_command,
        env,
    )

    wait_for_port(
        "127.0.0.1",
        VNC_PORT,
        timeout=60,
        process_name="x11vnc",
    )

    print(
        "[LOG] VNC 服务启动成功",
        flush=True,
    )

    # ========================================================
    # 9. 启动 noVNC WebSocket 网关
    # ========================================================

    websockify_command = [
        "/usr/bin/websockify",
        f"--web=/usr/share/novnc",
        str(WEB_PORT),
        f"127.0.0.1:{VNC_PORT}",
    ]

    start_process(
        "Websockify",
        websockify_command,
        env,
    )

    # ========================================================
    # 10. HTTP 健康检查
    # ========================================================

    health_url = (
        f"http://127.0.0.1:{WEB_PORT}/vnc.html"
    )

    wait_for_http(
        health_url,
        timeout=60,
    )

    print(
        "[LOG] noVNC HTTP 健康检查通过",
        flush=True,
    )

    # ========================================================
    # 11. 启动 Volume 定时提交
    # ========================================================

    start_commit_thread()

    # ========================================================
    # 12. 部署完成
    # ========================================================

    print("=" * 60, flush=True)
    print("[LOG] Chromium 部署启动成功", flush=True)
    print(f"[LOG] Web 端口：{WEB_PORT}", flush=True)
    print(f"[LOG] VNC 内部端口：{VNC_PORT}", flush=True)
    print(f"[LOG] 浏览器数据目录：{CONFIG_DIR}", flush=True)
    print("[LOG] Modal 代理身份验证已启用", flush=True)
    print("=" * 60, flush=True)
