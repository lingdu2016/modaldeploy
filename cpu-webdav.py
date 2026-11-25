import os
import socket
import subprocess
import threading
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "webdav"
VOLUME_NAME = "webdav-data"      # 独立的 Volume，不要和 S2 的 s2-data 共用

# 编译 webdav 用的 Go 镜像（GOTOOLCHAIN=auto 会在版本不够时自动下载工具链）
GO_IMAGE = "golang:1.26-bookworm"

# 想固定版本：把 @latest 改成具体 tag，例如 @v5.8.0
WEBDAV_INSTALL_TARGET = "github.com/hacdias/webdav/v5@latest"

# ---------- 服务配置 ----------
WEB_PORT = 6065                  # WebDAV 监听端口
DATA_DIR = "/data"               # 挂载 Volume 的目录，也是 WebDAV 的根目录
CONFIG_PATH = "/tmp/webdav.yml"  # 启动时生成的配置文件位置

# 用户权限：C=创建 R=读取 U=更新 D=删除，可组合
#   "CRUD" 完全读写；"R" 只读
PERMISSIONS = "CRUD"

# ---------- Modal 资源配置 ----------
MAX_CONTAINERS = 1               # 数据存在 Volume，必须单实例，避免多实例写冲突
MAX_CONCURRENT_INPUTS = 16
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 60

CPU = 0.125
MEMORY_MB = 128

# Volume 自动提交间隔（秒），防止容器回收时丢数据
VOLUME_COMMIT_INTERVAL_SECONDS = 30

# Modal Secret 名称，里面需要包含：
#   WEBDAV_USER       （必填）WebDAV 登录用户名
#   WEBDAV_PASSWORD   （必填）WebDAV 登录密码
SECRET_NAME = "webdav-secret"
# ====================================================


# ====================================================
# 1. 持久化数据卷（WebDAV 的所有文件都存在这里）
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像：Go 环境 + 构建期编译 webdav
# ====================================================
# 编译结果写进镜像层，冷启动时不用重新编译。
# add_python 是为了让 Modal 能在这个镜像里运行 Python 函数。
image = (
    modal.Image.from_registry(GO_IMAGE, add_python="3.12")
    .env({"GOTOOLCHAIN": "auto", "GOBIN": "/usr/local/bin"})
    .run_commands(f"go install {WEBDAV_INSTALL_TARGET}")
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
# 4. 启动 webdav
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
    """启动 WebDAV 服务。"""
    print("======================================", flush=True)
    print("正在启动 WebDAV...", flush=True)
    print("======================================", flush=True)

    # ---------- 读取并检查 Secret ----------
    webdav_user = os.environ.get("WEBDAV_USER", "").strip()
    webdav_password = os.environ.get("WEBDAV_PASSWORD", "").strip()

    # 没有账号密码等于公网公开，所以这里强制要求
    if not webdav_user or not webdav_password:
        raise ValueError(
            "WEBDAV_USER 和 WEBDAV_PASSWORD 未设置，请在 Modal Secret 中配置。"
        )

    # ---------- 准备数据目录 ----------
    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 生成配置文件 ----------
    # 账号密码用 {env}变量名 的写法，让 webdav 启动时从环境变量读取，
    # 这样密码不会写进配置文件
    config_text = f"""address: 0.0.0.0
port: {WEB_PORT}
auth: true
tls: false
prefix: /
directory: {DATA_DIR}
permissions: {PERMISSIONS}
behindProxy: true
users:
  - username: "{{env}}WEBDAV_USER"
    password: "{{env}}WEBDAV_PASSWORD"
"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(config_text)

    print(f"用户名：{webdav_user}（密码已设置）", flush=True)
    print(f"数据目录：{DATA_DIR}", flush=True)
    print(f"权限：{PERMISSIONS}", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # ---------- 启动 webdav ----------
    env = os.environ.copy()
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")

    process = subprocess.Popen(["webdav", "-c", CONFIG_PATH], env=env)
    print(f"进程已启动，PID：{process.pid}", flush=True)

    # ---------- 后台定期提交 Volume ----------
    threading.Thread(target=commit_volume_periodically, daemon=True).start()

    # ---------- 等待端口开始监听 ----------
    start_time = time.monotonic()
    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"WebDAV 启动失败，退出代码：{return_code}")

        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"WebDAV 已就绪，监听端口：{WEB_PORT}", flush=True)
                return
        except OSError:
            time.sleep(0.5)

    raise RuntimeError("WebDAV 启动超时，请检查 Modal 容器日志")
