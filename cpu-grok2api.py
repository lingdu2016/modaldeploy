"""
Modal 云端 Chromium（Debian + Xvfb + x11vnc + noVNC）

部署前先创建 Secret（密码至少 8 位；VNC 协议只取前 8 位生效）：
    modal secret create chromium-secret CHROMIUM_VNC_PASSWORD='你的密码'

部署：
    modal deploy app.py
"""

import atexit
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import modal


# ============================================================
#                    配置区（集中管理）
# ============================================================

APP_NAME = "chromium"
VOLUME_NAME = "chromium-data"
SECRET_NAME = "chromium-secret"

CHROMIUM_BINARY = "/usr/bin/chromium"

# noVNC 网页端口（Modal 对外提供 HTTPS）和 VNC 内部端口
WEB_PORT = 3000
VNC_PORT = 5900

# 虚拟显示器
DISPLAY_NUM = 99
DISPLAY = f":{DISPLAY_NUM}"
SCREEN_WIDTH = 1600
SCREEN_HEIGHT = 900
SCREEN_DEPTH = 24

# Modal 资源
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 16
CPU = 0.5
MEMORY_MB = 1024
TIMEOUT_SECONDS = 86400
SCALEDOWN_WINDOW_SECONDS = 180
WEB_STARTUP_TIMEOUT_SECONDS = 300

TIMEZONE = "Asia/Shanghai"

# 浏览器启动后打开的网址，留空则打开新标签页
START_URL = ""

# True：丢弃 Chromium 的日志（dbus、GPU 等噪音很多）；排查崩溃时改成 False
CHROMIUM_QUIET_LOGS = True

# True：用 dbus-launch 包一层启动 Chromium，可消除部分 dbus 报错
USE_DBUS_LAUNCH = False

# 浏览器数据同步回 Volume 的间隔
COMMIT_INTERVAL_SECONDS = 300

# ---------- 路径 ----------
# Volume 挂载点；浏览器配置保存在 /config/profile
CONFIG_DIR = "/config"
VOLUME_PROFILE_DIR = f"{CONFIG_DIR}/profile"

# Chromium 实际运行在本地磁盘，避免 SQLite 等高频读写直接落在 Volume 上
LOCAL_HOME = "/tmp/chromium-home"
LOCAL_PROFILE_DIR = "/tmp/chromium-profile"
RUNTIME_DIR = "/tmp/runtime-chromium"

VNC_PASSWORD_FILE = "/tmp/.vnc-passwd"
NOVNC_DIR = "/usr/share/novnc"

# ---------- 浏览器启动参数 ----------
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--hide-crash-restore-bubble",
    "--password-store=basic",
    "--ozone-platform=x11",
    "--lang=zh-CN",
    "--log-level=3",
    "--start-maximized",
    "--window-position=0,0",
    f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
    f"--user-data-dir={LOCAL_PROFILE_DIR}",
]

# 同步到 Volume 时跳过的缓存和锁文件
RSYNC_EXCLUDES = [
    "Singleton*",
    "Cache/",
    "Code Cache/",
    "GPUCache/",
    "GrShaderCache/",
    "ShaderCache/",
    "DawnCache/",
    "GraphiteDawnCache/",
    "Crashpad/",
    "BrowserMetrics*",
    "Service Worker/CacheStorage/",
    "Service Worker/ScriptCache/",
    "lost+found/",
]

# noVNC 根路径入口页：自动跳转到 vnc.html，并自动连接、自适应窗口、断线重连
INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Chromium</title>
</head>
<body>
<script>
location.replace("/vnc.html?autoconnect=true&resize=scale&reconnect=true&reconnect_delay=2000&path=websockify");
</script>
<noscript><a href="/vnc.html">打开 Chromium</a></noscript>
</body>
</html>
"""


# ============================================================
#                    Volume 与镜像
# ============================================================

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "chromium",
        "xvfb",
        "x11vnc",
        "novnc",
        "websockify",
        "openbox",
        "x11-utils",
        "dbus-x11",
        "rsync",
        "fonts-noto-cjk",
        "fonts-noto-color-emoji",
        "fonts-liberation",
    )
    .env(
        {
            "TZ": TIMEZONE,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
#                    全局状态
# ============================================================

processes = {}
stop_event = threading.Event()
profile_ready = threading.Event()  # 配置恢复完成后才允许写回 Volume
commit_lock = threading.Lock()
shutdown_lock = threading.Lock()
shutdown_started = False


# ============================================================
#                    检查工具
# ============================================================

def port_is_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def check_processes_alive():
    """任何已启动的进程提前退出都视为启动失败（Chromium 由守护线程管理，不在此检查）。"""
    for name, process in processes.items():
        if name == "Chromium":
            continue
        if process.poll() is not None:
            raise RuntimeError(
                f"{name} 进程提前退出，退出代码：{process.returncode}"
            )


def wait_for_port(host, port, timeout=60):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if port_is_open(host, port):
            return
        check_processes_alive()
        time.sleep(1)
    raise TimeoutError(f"等待端口超时：{host}:{port}")


def wait_for_http(url, timeout=60):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Modal-Chromium-HealthCheck"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        check_processes_alive()
        time.sleep(1)
    raise TimeoutError(f"HTTP 健康检查超时：{url}")


def wait_for_display(env, timeout=60):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        check_processes_alive()
        try:
            result = subprocess.run(
                ["xdpyinfo", "-display", DISPLAY],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(1)
    raise TimeoutError("X11 虚拟显示器启动超时。")


# ============================================================
#                    进程管理
# ============================================================

def start_process(name, command, env, quiet=False):
    print(f"[LOG] 正在启动 {name}...", flush=True)

    output = subprocess.DEVNULL if quiet else None

    process = subprocess.Popen(
        command,
        env=env,
        stdout=output,
        stderr=output,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    processes[name] = process
    print(f"[LOG] {name} 已启动，PID：{process.pid}", flush=True)
    return process


def stop_process(name, process):
    if process.poll() is not None:
        return

    print(f"[LOG] 正在关闭 {name}...", flush=True)

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        print(f"[LOG] {name} 未及时退出，强制终止", flush=True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ============================================================
#                    VNC 密码与入口页
# ============================================================

def create_vnc_password_file():
    """从 Modal Secret 读取密码并写入 x11vnc 密码文件。"""

    password = os.environ.get("CHROMIUM_VNC_PASSWORD", "").strip()

    if len(password) < 8:
        raise ValueError(
            "CHROMIUM_VNC_PASSWORD 未设置或少于 8 个字符。"
        )

    if len(password) > 8:
        print(
            "[WARN] VNC 协议只取密码前 8 位，超出部分不起作用",
            flush=True,
        )

    fd = os.open(
        VNC_PASSWORD_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(password + "\n")

    print("[LOG] VNC 密码文件已生成", flush=True)


def create_index_html():
    with open(
        os.path.join(NOVNC_DIR, "index.html"),
        "w",
        encoding="utf-8",
    ) as file:
        file.write(INDEX_HTML)


# ============================================================
#           浏览器配置：本地运行，定期同步回 Volume
# ============================================================

def run_rsync(src, dst, extra_excludes=()):
    os.makedirs(dst, exist_ok=True)

    command = ["/usr/bin/rsync", "-rlt", "--omit-dir-times", "--delete"]
    for pattern in (*RSYNC_EXCLUDES, *extra_excludes):
        command.append(f"--exclude={pattern}")
    command += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    # 24 表示同步过程中有文件消失，对运行中的浏览器配置属于正常现象
    if result.returncode not in (0, 24):
        print(
            f"[ERROR] rsync 失败（{result.returncode}）：{result.stderr.strip()}",
            flush=True,
        )
        return False

    return True


def restore_profile():
    """启动时把 Volume 中的配置恢复到本地磁盘。"""

    os.makedirs(LOCAL_PROFILE_DIR, mode=0o700, exist_ok=True)

    if os.path.isdir(VOLUME_PROFILE_DIR):
        print("[LOG] 正在从 Volume 恢复浏览器配置...", flush=True)
        ok = run_rsync(VOLUME_PROFILE_DIR, LOCAL_PROFILE_DIR)

    elif os.path.isdir(os.path.join(CONFIG_DIR, "Default")):
        # 兼容旧版本：配置直接保存在 /config 根目录
        print("[LOG] 检测到旧版配置，正在迁移...", flush=True)
        ok = run_rsync(
            CONFIG_DIR,
            LOCAL_PROFILE_DIR,
            extra_excludes=("profile/",),
        )

    else:
        print("[LOG] 未发现已有配置，使用全新配置", flush=True)
        ok = True

    # 恢复失败时直接中止，避免空配置反过来覆盖 Volume
    if not ok:
        raise RuntimeError("浏览器配置恢复失败。")

    profile_ready.set()


def commit_volume():
    """把本地配置同步到 Volume 并提交。"""

    if not profile_ready.is_set():
        print("[LOG] 配置尚未恢复，跳过提交", flush=True)
        return

    with commit_lock:
        try:
            if run_rsync(LOCAL_PROFILE_DIR, VOLUME_PROFILE_DIR):
                vol.commit()
                print(f"[LOG] {VOLUME_NAME} 已成功提交", flush=True)
        except Exception as error:
            print(f"[ERROR] Volume 提交失败：{error}", flush=True)


def commit_loop():
    while not stop_event.wait(COMMIT_INTERVAL_SECONDS):
        commit_volume()


# ============================================================
#                    Chromium 守护
# ============================================================

def clean_singleton_locks():
    """清理上次异常退出遗留的锁，避免提示"配置文件正在使用"。"""

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(LOCAL_PROFILE_DIR, name))
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"[WARN] 清理 {name} 失败：{error}", flush=True)


def build_chromium_command():
    command = [CHROMIUM_BINARY, *CHROMIUM_ARGS]

    if START_URL:
        command.append(START_URL)

    if USE_DBUS_LAUNCH and shutil.which("dbus-launch"):
        command = ["dbus-launch", "--exit-with-session", *command]

    return command


def chromium_supervisor(env):
    """Chromium 崩溃或被关闭窗口后自动重启，连续快速崩溃时逐步延长等待。"""

    restart_delay = 3

    while not stop_event.is_set():
        clean_singleton_locks()
        started_at = time.monotonic()

        process = start_process(
            "Chromium",
            build_chromium_command(),
            env,
            quiet=CHROMIUM_QUIET_LOGS,
        )

        while not stop_event.wait(2):
            if process.poll() is not None:
                break

        if stop_event.is_set():
            break

        runtime = time.monotonic() - started_at
        restart_delay = 3 if runtime >= 30 else min(restart_delay * 2, 60)

        print(
            f"[LOG] Chromium 已退出（退出代码 {process.returncode}），"
            f"{restart_delay} 秒后重启",
            flush=True,
        )

        if stop_event.wait(restart_delay):
            break


# ============================================================
#                    优雅关闭
# ============================================================

def shutdown():
    global shutdown_started

    with shutdown_lock:
        if shutdown_started:
            return
        shutdown_started = True

    print("[LOG] 正在执行优雅关闭...", flush=True)

    # 先通知守护线程和提交线程退出，防止关闭 Chromium 后被重启
    stop_event.set()

    for name in ("Websockify", "x11vnc", "Chromium", "Openbox", "Xvfb"):
        process = processes.get(name)
        if process:
            stop_process(name, process)

    # 浏览器关闭后再做最后一次提交，此时数据是一致的
    print("[LOG] 正在执行 Volume 最终提交...", flush=True)
    commit_volume()

    print("[LOG] 已关闭", flush=True)


def handle_signal(signum, frame):
    print(f"[LOG] 收到关闭信号：{signum}", flush=True)
    shutdown()
    raise SystemExit(0)


# ============================================================
#                    启动全部服务
# ============================================================

def start_services():
    print("=" * 60, flush=True)
    print("[LOG] 正在启动 Modal Chromium...", flush=True)
    print("=" * 60, flush=True)

    # 1. 检查必要程序
    for program in (
        CHROMIUM_BINARY,
        "/usr/bin/Xvfb",
        "/usr/bin/x11vnc",
        "/usr/bin/openbox",
        "/usr/bin/websockify",
        "/usr/bin/xdpyinfo",
        "/usr/bin/rsync",
    ):
        if not os.path.isfile(program):
            raise FileNotFoundError(f"找不到必要程序：{program}")

    if not os.path.isdir(NOVNC_DIR):
        raise FileNotFoundError(f"找不到 noVNC 目录：{NOVNC_DIR}")

    print("[LOG] 必要程序检查通过", flush=True)

    # 2. 检查 Secret 并生成 VNC 密码文件
    create_vnc_password_file()

    # 3. 创建目录
    for path in (LOCAL_HOME, RUNTIME_DIR):
        os.makedirs(path, mode=0o700, exist_ok=True)

    # 4. 注册退出处理
    atexit.register(shutdown)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except ValueError:
        # 不在主线程时无法注册信号，依赖 atexit 和定时提交
        print("[WARN] 无法注册信号处理器", flush=True)

    # 5. 子进程环境变量
    env = os.environ.copy()
    env.update(
        {
            "DISPLAY": DISPLAY,
            "HOME": LOCAL_HOME,
            "XDG_RUNTIME_DIR": RUNTIME_DIR,
            "TZ": TIMEZONE,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
    )

    # 6. 从 Volume 恢复浏览器配置
    restore_profile()

    # 7. Xvfb 虚拟显示器
    start_process(
        "Xvfb",
        [
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
        ],
        env,
    )
    wait_for_display(env)
    print("[LOG] X11 虚拟显示器启动成功", flush=True)

    # 8. Openbox 窗口管理器
    start_process("Openbox", ["/usr/bin/openbox", "--sm-disable"], env, quiet=True)
    time.sleep(2)
    check_processes_alive()

    # 9. 生成 noVNC 入口页
    create_index_html()

    # 10. 启动 Chromium（由守护线程管理）
    threading.Thread(
        target=chromium_supervisor,
        args=(env,),
        name="chromium-supervisor",
        daemon=True,
    ).start()

    time.sleep(5)

    chromium = processes.get("Chromium")
    if chromium is None or chromium.poll() is not None:
        raise RuntimeError("Chromium 启动失败，可将 CHROMIUM_QUIET_LOGS 改为 False 查看日志。")

    print("[LOG] Chromium 启动成功", flush=True)

    # 11. x11vnc（只监听本地，使用密码认证）
    start_process(
        "x11vnc",
        [
            "/usr/bin/x11vnc",
            "-display",
            DISPLAY,
            "-forever",
            "-shared",
            "-rfbport",
            str(VNC_PORT),
            "-localhost",
            "-passwdfile",
            VNC_PASSWORD_FILE,
            "-quiet",
            "-nowf",
            "-noscr",
            "-wait",
            "10",
            "-defer",
            "10",
            "-repeat",
            "-xkb",
        ],
        env,
    )
    wait_for_port("127.0.0.1", VNC_PORT)
    print("[LOG] VNC 服务启动成功", flush=True)

    # 12. websockify 提供 noVNC 网页和 WebSocket 网关
    start_process(
        "Websockify",
        [
            "/usr/bin/websockify",
            f"--web={NOVNC_DIR}",
            "--heartbeat=30",
            str(WEB_PORT),
            f"127.0.0.1:{VNC_PORT}",
        ],
        env,
    )
    wait_for_http(f"http://127.0.0.1:{WEB_PORT}/vnc.html")
    print("[LOG] noVNC HTTP 健康检查通过", flush=True)

    # 13. 定时同步浏览器配置
    threading.Thread(
        target=commit_loop,
        name="volume-commit",
        daemon=True,
    ).start()

    print("=" * 60, flush=True)
    print("[LOG] Chromium 部署启动成功", flush=True)
    print(f"[LOG] Web 端口：{WEB_PORT}", flush=True)
    print(f"[LOG] 浏览器配置：{LOCAL_PROFILE_DIR} -> {VOLUME_PROFILE_DIR}", flush=True)
    print("[LOG] VNC 密码认证已启用", flush=True)
    print("=" * 60, flush=True)


# ============================================================
#                    Modal 入口
# ============================================================

@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={CONFIG_DIR: vol},
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    try:
        start_services()
    except BaseException as error:
        print(f"[ERROR] 启动失败：{error}", flush=True)
        shutdown()
        raise
