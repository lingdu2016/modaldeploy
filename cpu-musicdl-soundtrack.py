
import os
import sys
import time
import subprocess
import urllib.request
from pathlib import Path

import modal


# ============================================================
# 配置区：所有可调整参数集中在这里
# ============================================================

# ---------- Modal 应用 ----------
APP_NAME = "soundtrack"
VOLUME_NAME = "soundtrack-data"

# ---------- GitHub 官方仓库 ----------
REPO_URL = "https://github.com/LiuKay/musicdl-soundtrack.git"
GIT_BRANCH = "main"

# ---------- 容器目录 ----------
WORK_DIR = "/opt/musicdl-soundtrack"

# 持久化目录：歌曲、下载历史、用户数据
DATA_DIR = "/data"
DOWNLOAD_DIR = f"{DATA_DIR}/downloads"
STATE_DIR = f"{DATA_DIR}/state"
HOME_DIR = f"{DATA_DIR}/home"
XDG_DATA_DIR = f"{HOME_DIR}/.local/share"

# 临时目录：音频缓存、musicdl 工作文件
# 容器销毁后会清空，不影响已经下载的歌曲
CACHE_DIR = "/tmp/soundtrack-cache"

# ---------- Web 服务 ----------
WEB_PORT = 5000
WEB_STARTUP_TIMEOUT_SECONDS = 180

# False：不要求 Modal Proxy Token。
# 注意：这样任何获得访问链接的人都可以访问应用。
REQUIRE_MODAL_AUTH = False

# ---------- Modal 资源 ----------
CPU = 0.5
MEMORY_MB = 512

# 最多同时运行一个容器
MAX_CONTAINERS = 1

# 每个容器最多同时处理 4 个请求
MAX_CONCURRENT_INPUTS = 4

# 单次请求最长执行时间
TIMEOUT_SECONDS = 3600

# 空闲 120 秒后自动缩容
SCALEDOWN_WINDOW_SECONDS = 120

# ---------- Python ----------
PYTHON_VERSION = "3.11"


# ============================================================
# 创建 Modal Volume
# ============================================================

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
# 构建镜像
# ============================================================

# 镜像在部署时构建，后续冷启动直接使用构建好的镜像。
# Git 和 FFmpeg 在镜像构建阶段安装，不在每次启动时安装。

image = (
    modal.Image.debian_slim(
        python_version=PYTHON_VERSION
    )
    .apt_install(
        "git",
        "ffmpeg",
    )
    .run_commands(
        f"git clone --depth 1 --branch {GIT_BRANCH} "
        f"{REPO_URL} {WORK_DIR}",
    )
    .run_commands(
        f"python -m pip install --no-cache-dir "
        f"-r {WORK_DIR}/requirements.txt",
    )
)


# ============================================================
# 创建 Modal App
# ============================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
# 修改官方 Flask 源码，使其适配 Modal
# ============================================================

def patch_soundtrack_source():

    app_file = Path(WORK_DIR) / "app.py"

    if not app_file.is_file():
        raise FileNotFoundError(
            f"没有找到项目入口文件：{app_file}"
        )

    source = app_file.read_text(
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # 修改 1：允许 Modal 的域名访问
    #
    # 官方源码默认只允许 localhost。
    # Modal 代理访问时使用的是 Modal 域名，因此需要调整。
    # --------------------------------------------------------

    old_guard = (
        "    if hostname not in {'127.0.0.1', 'localhost', '::1'}:\n"
        "        return '', 403"
    )

    new_guard = (
        "    if (hostname not in {'127.0.0.1', 'localhost', '::1'} "
        "and not hostname.endswith('.modal.run')):\n"
        "        return '', 403"
    )

    if old_guard in source:

        source = source.replace(
            old_guard,
            new_guard,
            1,
        )

    elif new_guard not in source:

        raise RuntimeError(
            "无法匹配官方源码中的 Host 检查代码，"
            "请检查 GitHub 项目是否发生了变化。"
        )

    # --------------------------------------------------------
    # 修改 2：Flask 监听 0.0.0.0
    #
    # Modal 需要从容器外部访问 Flask 服务。
    # --------------------------------------------------------

    old_host = (
        "app.run(host='127.0.0.1', port=port, "
        "threaded=True, debug=False)"
    )

    new_host = (
        "app.run(host='0.0.0.0', port=port, "
        "threaded=True, debug=False)"
    )

    if old_host in source:

        source = source.replace(
            old_host,
            new_host,
            1,
        )

    elif new_host not in source:

        raise RuntimeError(
            "无法匹配官方 Flask 启动代码，"
            "请检查 GitHub 项目是否发生了变化。"
        )

    # 保存修改后的源码
    app_file.write_text(
        source,
        encoding="utf-8",
    )

    print("Soundtrack 官方源码适配完成")


# ============================================================
# 启动 Web 服务
# ============================================================

@app.function(
    cpu=CPU,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,

    # 将 Modal Volume 挂载到 /data
    volumes={
        DATA_DIR: vol,
    },
)

@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS
)

@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    requires_proxy_auth=REQUIRE_MODAL_AUTH,
)

def ui():

    # --------------------------------------------------------
    # 1. 创建目录
    # --------------------------------------------------------

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(HOME_DIR, exist_ok=True)
    os.makedirs(XDG_DATA_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # --------------------------------------------------------
    # 2. 设置环境变量
    #
    # 以下变量均与官方 app.py 中实际读取的配置对应。
    # --------------------------------------------------------

    env = os.environ.copy()

    env["TZ"] = "Asia/Shanghai"

    env["PORT"] = str(WEB_PORT)

    # 下载歌曲保存到 Volume
    env["SOUNDTRACK_DOWNLOAD_DIR"] = DOWNLOAD_DIR

    # 下载历史保存到 Volume
    env["SOUNDTRACK_STATE_DIR"] = STATE_DIR

    # 音频缓存及 musicdl 工作目录使用临时磁盘
    env["SOUNDTRACK_CACHE_DIR"] = CACHE_DIR

    # 持久化用户目录及废纸篓数据
    env["HOME"] = HOME_DIR
    env["XDG_DATA_HOME"] = XDG_DATA_DIR

    # 实时输出日志
    env["PYTHONUNBUFFERED"] = "1"

    # --------------------------------------------------------
    # 3. 适配官方源码
    # --------------------------------------------------------

    patch_soundtrack_source()

    # --------------------------------------------------------
    # 4. 启动 Flask
    # --------------------------------------------------------

    print("正在启动 Soundtrack...")

    process = subprocess.Popen(
        [
            sys.executable,
            "app.py",
        ],
        cwd=WORK_DIR,
        env=env,
    )

    # --------------------------------------------------------
    # 5. 检查服务是否正常启动
    #
    # 通过访问本地首页判断 Flask 是否就绪。
    # --------------------------------------------------------

    health_url = (
        f"http://127.0.0.1:{WEB_PORT}/"
    )

    deadline = (
        time.monotonic()
        + WEB_STARTUP_TIMEOUT_SECONDS
    )

    last_error = None

    while time.monotonic() < deadline:

        # Flask 进程提前退出，立即报告错误
        if process.poll() is not None:

            raise RuntimeError(
                f"Soundtrack 启动失败，"
                f"进程退出代码：{process.returncode}"
            )

        try:

            with urllib.request.urlopen(
                health_url,
                timeout=3,
            ) as response:

                if response.status == 200:

                    print(
                        "Soundtrack 启动成功"
                    )

                    print(
                        f"下载目录：{DOWNLOAD_DIR}"
                    )

                    print(
                        f"状态目录：{STATE_DIR}"
                    )

                    print(
                        f"缓存目录：{CACHE_DIR}"
                    )

                    return

        except Exception as error:

            last_error = error

        time.sleep(2)

    # --------------------------------------------------------
    # 6. 超时处理
    # --------------------------------------------------------

    if process.poll() is None:

        process.terminate()

        try:
            process.wait(timeout=10)

        except subprocess.TimeoutExpired:

            process.kill()

    raise TimeoutError(
        "Soundtrack 启动超时，"
        f"最后一次健康检查错误：{last_error}"
    )
