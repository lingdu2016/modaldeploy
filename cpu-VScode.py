import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "code-server"
VOLUME_NAME = "code-server-data"

# 官方镜像（见 https://github.com/coder/code-server 的 docs/install.md 的 Docker 小节）
# GHCR: ghcr.io/coder/code-server    Docker Hub: codercom/code-server
# 建议固定版本 tag（例如 "ghcr.io/coder/code-server:4.99.4"），避免 :latest 变化导致镜像层重建
IMAGE = "ghcr.io/coder/code-server:latest"
WEB_PORT = 8080                       # 官方镜像默认监听 8080

# 官方镜像的用户目录。官方 Docker 部署要持久化的三处：
#   /home/coder/.local   扩展、用户数据
#   /home/coder/.config  config.yaml
#   /home/coder/project  项目代码
# 这里把整个 /home/coder 挂载为一个 Volume，一次性全部持久化。
HOME_DIR = "/home/coder"
WORKSPACE_DIR = f"{HOME_DIR}/project"

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 100           # code-server 依赖 WebSocket 长连接
TIMEOUT_SECONDS = 6 * 3600            # 单个容器最长存活时间（Modal 上限 24h）
SCALEDOWN_WINDOW_SECONDS = 600
WEB_STARTUP_TIMEOUT_SECONDS = 120

CPU = 1                             # 官方 TL;DR：1 GB 内存 + 2 vCPU 起步
MEMORY_MB = 1024

# Secret 中需要有 PASSWORD（明文）或 HASHED_PASSWORD（SHA-256 / argon2 哈希）之一
SECRET_NAME = "code-server-secret"

# 首次启动时自动安装的扩展（可留空），例如 ["ms-python.python"]
EXTENSIONS: list[str] = []
# ====================================================


# ====================================================
# 1. 持久化数据卷（对应官方 Docker 示例里的 .local / .config / project 挂载）
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像：官方镜像 + Modal 所需的 Python
# ====================================================
# 官方镜像的 ENTRYPOINT 是 /usr/bin/entrypoint.sh（含 fixuid / DOCKER_USER 逻辑，
# 面向非 root 容器），在 Modal 里清掉它，直接调用 /usr/bin/code-server。
image = (
    modal.Image.from_registry(IMAGE, add_python="3.12")
    .entrypoint([])
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 启动 code-server
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={HOME_DIR: vol},
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
    """启动 code-server Web 服务。"""
    print("======================================", flush=True)
    print("正在启动 code-server...", flush=True)
    print("======================================", flush=True)

    password = os.environ.get("PASSWORD", "").strip()
    hashed_password = os.environ.get("HASHED_PASSWORD", "").strip()

    # code-server 在 auth=password 时，必须通过 $PASSWORD 或 $HASHED_PASSWORD 提供密码
    if not password and not hashed_password:
        raise ValueError(
            "PASSWORD / HASHED_PASSWORD 均未设置。请在 Modal Secret 中配置："
            f"modal secret create {SECRET_NAME} PASSWORD=你的密码"
        )

    config_dir = f"{HOME_DIR}/.config/code-server"
    user_data_dir = f"{HOME_DIR}/.local/share/code-server"
    extensions_dir = f"{user_data_dir}/extensions"
    for d in (config_dir, extensions_dir, WORKSPACE_DIR):
        os.makedirs(d, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = HOME_DIR
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")
    if password:
        env["PASSWORD"] = password
    if hashed_password:
        env["HASHED_PASSWORD"] = hashed_password

    print(f"当前 UID：{os.getuid()}", flush=True)
    print(f"密码：{'HASHED_PASSWORD' if hashed_password else 'PASSWORD'} 已设置", flush=True)
    print(f"HOME：{HOME_DIR}", flush=True)
    print(f"配置目录：{config_dir}", flush=True)
    print(f"数据目录：{user_data_dir}", flush=True)
    print(f"工作区：{WORKSPACE_DIR}", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # 首次启动安装扩展（用标记文件保证幂等）
    marker = f"{user_data_dir}/.extensions_installed"
    if EXTENSIONS and not os.path.exists(marker):
        for ext in EXTENSIONS:
            print(f"安装扩展：{ext}", flush=True)
            subprocess.run(
                [
                    "/usr/bin/code-server",
                    "--user-data-dir", user_data_dir,
                    "--extensions-dir", extensions_dir,
                    "--install-extension", ext,
                ],
                env=env,
                check=False,
                timeout=300,
            )
        open(marker, "w").close()

    cmd = [
        "/usr/bin/code-server",
        "--bind-addr", f"0.0.0.0:{WEB_PORT}",
        "--auth", "password",
        "--user-data-dir", user_data_dir,
        "--extensions-dir", extensions_dir,
        "--disable-telemetry",
        WORKSPACE_DIR,
    ]

    process = subprocess.Popen(cmd, env=env, cwd=HOME_DIR)

    print(f"进程已启动，PID：{process.pid}", flush=True)

    time.sleep(3)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(f"code-server 启动失败，退出代码：{return_code}")

    print("进程仍在运行，等待 Web 服务就绪。", flush=True)
