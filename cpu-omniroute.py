import os
import subprocess
import time
import socket
import threading
import atexit
import modal

# ====================== 配置区 ======================
APP_NAME = "omniroute"
VOLUME_NAME = "omniroute-data"

# 官方镜像（可改成固定版本，如 diegosouzapw/omniroute:3.8.51）
IMAGE = "diegosouzapw/omniroute:latest"

WEB_PORT = 20128
DATA_DIR = "/app/data"

MAX_CONTAINERS = 1          # SQLite 必须单实例
MAX_CONCURRENT_INPUTS = 16
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 600
WEB_STARTUP_TIMEOUT_SECONDS = 300

# Node 网关比 qwen2api 重，建议至少 0.5C / 1G
CPU = 0.5
MEMORY_MB = 1024
# Node 堆上限（建议 ≤ MEMORY_MB - 256）
OMNIROUTE_MEMORY_MB = 768

SECRET_NAME = "omniroute-secret"
# ====================================================


vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


# ====================================================
# 镜像：官方 Debian + 安装 Python + 覆盖 ENTRYPOINT
# ====================================================
image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        # 官方是 node:*-trixie-slim（Debian），用 apt
        "RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip "
        "&& ln -sf /usr/bin/python3 /usr/bin/python "
        "&& ln -sf /usr/bin/pip3 /usr/bin/pip "
        "&& rm -rf /var/lib/apt/lists/*",
    ],
)

# 官方 ENTRYPOINT 多为 /app/check-permissions.sh
# 必须用 shell 转交 Modal 的 Python 启动参数
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


app = modal.App(name=APP_NAME, image=image)


# ====================================================
# Volume 自动提交
# ====================================================
def commit_worker():
    while True:
        time.sleep(30)
        try:
            vol.commit()
            print("[LOG] Volume 自动提交成功", flush=True)
        except Exception as e:
            print(f"[LOG] Volume 自动提交失败：{e}", flush=True)


def commit_on_exit():
    try:
        vol.commit()
        print("[LOG] Volume 最终提交成功", flush=True)
    except Exception as e:
        print(f"[LOG] Volume 最终提交失败：{e}", flush=True)


# ====================================================
# 启动 OmniRoute
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={DATA_DIR: vol},
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
    print("==================================================", flush=True)
    print("[LOG] 正在启动 OmniRoute...", flush=True)
    print("==================================================", flush=True)

    # ---------- Secret（建议都设） ----------
    jwt_secret = os.environ.get("JWT_SECRET", "").strip()
    api_key_secret = os.environ.get("API_KEY_SECRET", "").strip()
    initial_password = os.environ.get("INITIAL_PASSWORD", "").strip()
    ws_bridge_secret = os.environ.get("OMNIROUTE_WS_BRIDGE_SECRET", "").strip()

    if not jwt_secret:
        raise ValueError("JWT_SECRET 未设置，请在 Modal Secret 中配置。")
    if not api_key_secret:
        raise ValueError("API_KEY_SECRET 未设置，请在 Modal Secret 中配置。")

    print("[LOG] ✅ JWT_SECRET / API_KEY_SECRET 已加载", flush=True)
    if initial_password:
        print("[LOG] ✅ INITIAL_PASSWORD 已设置（仅首次生效）", flush=True)
    else:
        print("[LOG] ⚠ INITIAL_PASSWORD 未设置，首次访问走引导向导", flush=True)

    # ---------- 数据目录 ----------
    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 环境变量 ----------
    env = os.environ.copy()
    env["NODE_ENV"] = "production"
    env["PORT"] = str(WEB_PORT)
    env["HOSTNAME"] = "0.0.0.0"
    env["DATA_DIR"] = DATA_DIR
    env["JWT_SECRET"] = jwt_secret
    env["API_KEY_SECRET"] = api_key_secret
    env["OMNIROUTE_MEMORY_MB"] = str(OMNIROUTE_MEMORY_MB)
    env["NODE_OPTIONS"] = f"--max-old-space-size={OMNIROUTE_MEMORY_MB}"

    if initial_password:
        env["INITIAL_PASSWORD"] = initial_password
    if ws_bridge_secret:
        env["OMNIROUTE_WS_BRIDGE_SECRET"] = ws_bridge_secret

    # 公网访问时建议打开 API Key（可在 Secret 里再覆盖）
    env.setdefault("REQUIRE_API_KEY", os.environ.get("REQUIRE_API_KEY", "true"))
    # Modal 是 HTTPS 终止，cookie 可按需设
    env.setdefault("AUTH_COOKIE_SECURE", os.environ.get("AUTH_COOKIE_SECURE", "true"))

    # ---------- Volume 提交 ----------
    threading.Thread(target=commit_worker, daemon=True).start()
    atexit.register(commit_on_exit)

    # ---------- 启动命令（官方 CMD） ----------
    # 不同版本可能是 run-standalone.mjs 或 dev/run-standalone.mjs
    candidates = [
        ["node", "run-standalone.mjs"],
        ["node", "dev/run-standalone.mjs"],
        ["node", "/app/run-standalone.mjs"],
        ["node", "/app/dev/run-standalone.mjs"],
    ]

    command = None
    for cmd in candidates:
        script = cmd[-1]
        # 相对路径以 /app 为 cwd
        path = script if script.startswith("/") else os.path.join("/app", script)
        if os.path.exists(path):
            command = cmd
            break

    if command is None:
        # 兜底：仍用官方默认
        command = ["node", "run-standalone.mjs"]
        print(f"[LOG] ⚠ 未找到脚本，尝试默认：{command}", flush=True)
    else:
        print(f"[LOG] 使用启动命令：{command}", flush=True)

    print("[LOG] 正在拉起 OmniRoute 进程...", flush=True)
    process = subprocess.Popen(
        command,
        env=env,
        cwd="/app",
        stdout=None,
        stderr=None,
    )
    print(f"[LOG] 进程已启动，PID：{process.pid}", flush=True)

    # ---------- 等待端口就绪 ----------
    start_time = time.monotonic()
    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"OmniRoute 启动失败，退出代码：{exit_code}")

        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"[LOG] ✅ 服务已就绪，监听端口：{WEB_PORT}", flush=True)
                break
        except OSError:
            time.sleep(0.5)
    else:
        raise RuntimeError("OmniRoute 启动超时，请检查容器日志")

    print("==================================================", flush=True)
    print("[LOG] OmniRoute 启动完成，Modal 已接管", flush=True)
    print(f"[LOG] Dashboard / API: 端口 {WEB_PORT}", flush=True)
    print("==================================================", flush=True)
