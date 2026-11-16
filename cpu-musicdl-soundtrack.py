
import os
import sys
import time
import socket
import threading
import subprocess
import urllib.request
from pathlib import Path

import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "soundtrack"
VOLUME_NAME = "soundtrack-data"

# ---------- 官方 GitHub 源码 ----------
REPO_URL = "https://github.com/LiuKay/musicdl-soundtrack.git"

# ---------- 服务配置 ----------
WEB_PORT = 5000
WORK_DIR = "/opt/musicdl-soundtrack"

# ---------- 持久化目录 ----------
DATA_DIR = "/data"
DOWNLOAD_DIR = f"{DATA_DIR}/downloads"
STATE_DIR = f"{DATA_DIR}/state"

# 播放缓存和 musicdl 工作目录使用临时存储
CACHE_DIR = "/tmp/soundtrack-cache"

# ---------- Modal 资源配置 ----------
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 4

CPU = 1.0
MEMORY_MB = 1024

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 120
WEB_STARTUP_TIMEOUT_SECONDS = 180

# ---------- Volume 配置 ----------
# 定期提交 Volume，防止容器休眠或回收时丢失数据
VOLUME_COMMIT_INTERVAL = 30

# ---------- 服务安全配置 ----------
# 使用 Modal Proxy Auth 保护 Web 服务
# 访问时需要通过 Modal 身份验证
REQUIRE_MODAL_AUTH = True

# ---------- 环境配置 ----------
PYTHON_VERSION = "3.11"
TIMEZONE = "Asia/Shanghai"

# ====================================================


# ====================== 创建 Modal 资源 ======================

# 创建持久化 Volume
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True
)

# 使用 Modal 原生 Python 镜像
# 安装 Git 和 FFmpeg
# FFmpeg / FFprobe 用于音频格式检查和 MP3 转换
image = (
    modal.Image.debian_slim(
        python_version=PYTHON_VERSION
    )
    .apt_install(
        "git",
        "ffmpeg",
    )
    # 克隆官方源码
    .run_commands(
        f"git clone --depth 1 {REPO_URL} {WORK_DIR}",
    )
    # 安装项目官方 requirements.txt
    .run_commands(
        f"python -m pip install --no-cache-dir -r {WORK_DIR}/requirements.txt",
    )
)

# 创建 Modal App
app = modal.App(
    name=APP_NAME,
    image=image
)

# ====================================================


# ====================== Volume 定期持久化 ======================

def commit_volume_periodically():

    """
    定期提交 Modal Volume。

    Soundtrack 的音乐文件、下载历史和状态
    都保存在 Volume 中。

    Modal Volume 需要主动 commit，
    才能将修改持久化。
    """

    while True:

        time.sleep(VOLUME_COMMIT_INTERVAL)

        try:

            vol.commit()

            print("Soundtrack Volume 已成功提交")

        except Exception as error:

            print(f"Volume 提交失败：{error}")


# ====================================================


# ====================== 启动 Soundtrack 服务 ======================

@app.function(
    cpu=CPU,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,
    volumes={
        DATA_DIR: vol
    }
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    requires_proxy_auth=REQUIRE_MODAL_AUTH
)
def ui():

    # ---------- 准备目录 ----------

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs("/data/home", exist_ok=True)
    os.makedirs("/data/xdg", exist_ok=True)

    # ---------- 环境变量 ----------

    env = os.environ.copy()

    env["TZ"] = TIMEZONE

    env["PORT"] = str(WEB_PORT)

    # 音乐下载文件持久化
    env["SOUNDTRACK_DOWNLOAD_DIR"] = DOWNLOAD_DIR

    # 下载历史和任务状态持久化
    env["SOUNDTRACK_STATE_DIR"] = STATE_DIR

    # 播放缓存使用临时目录
    env["SOUNDTRACK_CACHE_DIR"] = CACHE_DIR

    # 设置用户目录
    env["HOME"] = "/data/home"

    # 设置回收站数据目录
    env["XDG_DATA_HOME"] = "/data/xdg"

    # Python 日志直接输出
    env["PYTHONUNBUFFERED"] = "1"

    # ---------- 检查官方源码 ----------

    app_file = Path(WORK_DIR) / "app.py"

    if not app_file.is_file():

        raise RuntimeError(
            "Soundtrack 官方 app.py 不存在"
        )

    # ---------- Modal 兼容性修复 ----------

    source = app_file.read_text(encoding="utf-8")

    # 修复一：
    # 原项目只允许 localhost 请求。
    # Modal 通过 *.modal.run 域名访问时会被原项目拒绝。
    # 保留 localhost 检查，同时允许 Modal 官方域名。

    old_guard = (
        "    if hostname not in {'127.0.0.1', 'localhost', '::1'}:\n"
        "        return '', 403"
    )

    new_guard = (
        "    if hostname not in {'127.0.0.1', 'localhost', '::1'} "
        "and not hostname.endswith('.modal.run'):\n"
        "        return '', 403"
    )

    if old_guard not in source:

        raise RuntimeError(
            "无法匹配 Soundtrack 原始 Host 检查代码，"
            "请检查官方仓库是否修改了源码"
        )

    source = source.replace(
        old_guard,
        new_guard,
        1
    )

    # 修复二：
    # Flask 原本只监听 127.0.0.1。
    # 改为监听 0.0.0.0，使 Modal Web Server 可以转发请求。

    old_host = (
        "app.run(host='127.0.0.1', "
        "port=port, threaded=True, debug=False)"
    )

    new_host = (
        "app.run(host='0.0.0.0', "
        "port=port, threaded=True, debug=False)"
    )

    if old_host not in source:

        raise RuntimeError(
            "无法匹配 Soundtrack 原始 Flask 启动代码"
        )

    source = source.replace(
        old_host,
        new_host,
        1
    )

    # 保存修复后的源码
    app_file.write_text(
        source,
        encoding="utf-8"
    )

    print("Soundtrack Modal 兼容性修复完成")

    # ---------- 初始化 Volume ----------

    # 确保初始目录和文件结构能够持久化
    vol.commit()

    # ---------- 启动 Volume 定期提交线程 ----------

    commit_thread = threading.Thread(
        target=commit_volume_periodically,
        daemon=True
    )

    commit_thread.start()

    # ---------- 启动 Soundtrack ----------

    command = [
        sys.executable,
        "app.py",
    ]

    print("正在启动 Soundtrack...")

    print(f"源码目录：{WORK_DIR}")
    print(f"下载目录：{DOWNLOAD_DIR}")
    print(f"状态目录：{STATE_DIR}")
    print(f"缓存目录：{CACHE_DIR}")
    print(f"监听端口：{WEB_PORT}")
    print(f"CPU：{CPU}")
    print(f"内存：{MEMORY_MB} MB")

    process = subprocess.Popen(
        command,
        cwd=WORK_DIR,
        env=env
    )

    print(
        f"Soundtrack 进程 PID：{process.pid}"
    )

    # ---------- 等待 Web 服务启动 ----------

    start_time = time.monotonic()

    while (
        time.monotonic() - start_time
        < WEB_STARTUP_TIMEOUT_SECONDS
    ):

        # 检查进程是否异常退出
        exit_code = process.poll()

        if exit_code is not None:

            raise RuntimeError(
                f"Soundtrack 启动失败，"
                f"退出代码：{exit_code}"
            )

        # 检查 HTTP 服务是否正常响应
        try:

            request = urllib.request.Request(
                f"http://127.0.0.1:{WEB_PORT}/",
                headers={
                    "Host": f"127.0.0.1:{WEB_PORT}"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=3
            ) as response:

                if response.status == 200:

                    print(
                        "Soundtrack 已成功启动！"
                    )

                    print(
                        f"监听端口：{WEB_PORT}"
                    )

                    break

        except Exception:

            time.sleep(1)

    else:

        raise RuntimeError(
            "Soundtrack 启动超时，"
            "请检查 Modal 容器日志"
        )

# ====================================================
