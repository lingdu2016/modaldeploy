import os
import subprocess
import threading
import time
import socket
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "s2"
VOLUME_NAME = "s2-data"

# 编译 s2-server 用的 Go 镜像（s2 最新版要求 Go >= 1.26）
GO_IMAGE = "golang:1.26-bookworm"

# 想固定版本：把 @latest 改成具体 tag，例如 @v0.37.0
S2_INSTALL_TARGET = "github.com/mojatter/s2/cmd/s2-server@latest"

# ---------- 对外暴露哪个服务（Modal 一个函数只能暴露一个端口） ----------
#   "console" -> Web 控制台（浏览器登录，查看桶和文件，用来检查账号密码是否正确）
#   "s3"      -> S3 API（OpenList / aws cli / SDK 连接用）
EXPOSE = "console"

# S2 内部端口（一般不用改）
S3_PORT = 9000
CONSOLE_PORT = 9001

# ---------- 启动时自动创建的 bucket（逗号分隔） ----------
# 已存在的不会重复创建；删除这里的名字不会删除已有的桶
S2_BUCKETS = "assets,upload"

# S2 数据目录（挂载 Volume）
DATA_DIR = "/var/lib/s2"

MAX_CONTAINERS = 1          # osfs 存本地文件，必须单实例，避免多实例写冲突
MAX_CONCURRENT_INPUTS = 16
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 120

CPU = 0.25
MEMORY_MB = 256

# Volume 自动提交间隔（秒），防止容器回收时丢数据
VOLUME_COMMIT_INTERVAL_SECONDS = 30

# Modal Secret 名称，里面只需要两项：
#   S2_SERVER_USER       S3 Access Key / 控制台用户名
#   S2_SERVER_PASSWORD   S3 Secret Key / 控制台密码
SECRET_NAME = "s2-secret"
# ====================================================


# 根据 EXPOSE 决定对外暴露的端口
WEB_PORT = CONSOLE_PORT if EXPOSE == "console" else S3_PORT


# ====================================================
# 1. 持久化数据卷（存放所有 bucket 和对象数据）
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像：Go 环境 + 构建期编译 s2-server
# ====================================================
# 编译结果写进镜像层，冷启动时不用重新编译。
# add_python 是为了让 Modal 能在这个镜像里运行 Python 函数。
# GOTOOLCHAIN=auto：s2 要求的 Go 版本比镜像高时，自动下载匹配的工具链
image = (
    modal.Image.from_registry(GO_IMAGE, add_python="3.12")
    .env({"GOTOOLCHAIN": "auto"})
    .run_commands(
        f"go install {S2_INSTALL_TARGET}",
        "cp /go/bin/s2-server /usr/local/bin/s2-server",
    )
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 辅助：定期提交 Volume
# ====================================================
def commit_volume_periodically():
    """后台线程：每隔一段时间把容器内写入的数据提交到 Volume。"""
    while True:
        time.sleep(VOLUME_COMMIT_INTERVAL_SECONDS)
        try:
            vol.commit()
        except Exception as error:
            print(f"Volume 提交失败：{error}", flush=True)


# ====================================================
# 4. 启动 s2-server
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={DATA_DIR: vol},
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
    """启动 S2（S3 兼容对象存储）服务。"""
    print("======================================", flush=True)
    print("正在启动 S2...", flush=True)
    print("======================================", flush=True)

    # ---------- 读取并检查 Secret ----------
    s2_user = os.environ.get("S2_SERVER_USER", "").strip()
    s2_password = os.environ.get("S2_SERVER_PASSWORD", "").strip()

    # 不设账号密码 S2 会关闭认证，等于公网公开，所以这里强制要求
    if not s2_user or not s2_password:
        raise ValueError(
            "S2_SERVER_USER 和 S2_SERVER_PASSWORD 未设置，请在 Modal Secret 中配置。"
        )

    # ---------- 准备数据目录 ----------
    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 配置环境变量（S2 通过环境变量配置） ----------
    env = os.environ.copy()
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")
    env["S2_SERVER_LISTEN"] = f":{S3_PORT}"               # S3 API 监听地址
    env["S2_SERVER_CONSOLE_LISTEN"] = f":{CONSOLE_PORT}"  # Web 控制台监听地址
    env["S2_SERVER_TYPE"] = "osfs"                        # 存储后端：本地文件系统
    env["S2_SERVER_ROOT"] = DATA_DIR                      # 数据根目录
    env["S2_SERVER_USER"] = s2_user
    env["S2_SERVER_PASSWORD"] = s2_password
    env["S2_SERVER_BUCKETS"] = S2_BUCKETS                 # 启动时自动创建的桶

    print(f"当前模式：{EXPOSE}（{'Web 控制台' if EXPOSE == 'console' else 'S3 API'}）", flush=True)
    print(f"用户名：{s2_user}（长度 {len(s2_user)}），密码长度：{len(s2_password)}", flush=True)
    print(f"BUCKETS：{S2_BUCKETS}", flush=True)
    print(f"数据目录：{DATA_DIR}", flush=True)
    print(f"S3 API 端口：{S3_PORT}", flush=True)
    print(f"Web 控制台端口：{CONSOLE_PORT}", flush=True)
    print(f"对外暴露端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # ---------- 启动 s2-server ----------
    process = subprocess.Popen(["s2-server"], env=env)
    print(f"进程已启动，PID：{process.pid}", flush=True)

    # ---------- 后台定期提交 Volume ----------
    threading.Thread(target=commit_volume_periodically, daemon=True).start()

    # ---------- 等待对外端口开始监听 ----------
    start_time = time.monotonic()
    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"S2 启动失败，退出代码：{return_code}")

        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"S2 已就绪，监听端口：{WEB_PORT}", flush=True)
                return
        except OSError:
            time.sleep(0.5)

    raise RuntimeError("S2 启动超时，请检查 Modal 容器日志")
