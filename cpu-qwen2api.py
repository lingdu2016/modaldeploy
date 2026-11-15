import os
import subprocess
import time
import socket
import threading
import atexit
import modal

# ====================== 配置区 ======================
APP_NAME = "qwen2api"
VOLUME_NAME = "qwen2api-data"

IMAGE = "rfym21/qwen2api:latest"
WEB_PORT = 3000

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 90
WEB_STARTUP_TIMEOUT_SECONDS = 300   # 启动可能稍慢，保持较大

CPU = 0.25
MEMORY_MB = 256

SECRET_NAME = "qwen2api-secret"

# 数据目录（与官方 docker-compose 一致）
DATA_DIR = "/app/data"
# ====================================================


vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


# ====================================================
# 镜像：官方 Alpine + 安装 Python + 正确处理 ENTRYPOINT
# ====================================================
image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 关键修复：官方 ENTRYPOINT = ["/usr/local/bin/qwen2api"]
# 必须用 shell 把 Modal 的启动参数正确转交，否则会乱
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


app = modal.App(name=APP_NAME, image=image)


# ====================================================
# Volume 自动提交（可选，但推荐）
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
# 启动服务
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
    print("[LOG] 正在启动 Qwen2API...", flush=True)
    print("==================================================", flush=True)

    # ---------- 检查 Secret ----------
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise ValueError("API_KEY 未设置，请检查 Modal Secret。")
    print("[LOG] ✅ 成功加载 API_KEY", flush=True)

    # ---------- 准备目录 ----------
    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 环境变量 ----------
    env = os.environ.copy()
    env["API_KEY"] = api_key
    env["LISTEN_ADDRESS"] = "0.0.0.0"          # 必须监听 0.0.0.0
    env["SERVICE_PORT"] = str(WEB_PORT)
    env["DATA_SAVE_MODE"] = os.environ.get("DATA_SAVE_MODE", "file")
    env["CACHE_MODE"] = os.environ.get("CACHE_MODE", "file")
    env["OUTPUT_THINK"] = os.environ.get("OUTPUT_THINK", "true")
    env["SEARCH_INFO_MODE"] = os.environ.get("SEARCH_INFO_MODE", "table")
    # 如果 Secret 里还有 ACCOUNTS，会自动带进来

    # 可选：写一份 .env（部分逻辑会读）
    env_file = "/app/.env"
    with open(env_file, "w", encoding="utf-8") as f:
        for k in [
            "API_KEY", "LISTEN_ADDRESS", "SERVICE_PORT",
            "DATA_SAVE_MODE", "CACHE_MODE", "OUTPUT_THINK", "SEARCH_INFO_MODE"
        ]:
            if k in env:
                f.write(f"{k}={env[k]}\n")
    print("[LOG] ✅ 运行时配置已写入", flush=True)

    # ---------- Volume 提交线程 ----------
    threading.Thread(target=commit_worker, daemon=True).start()
    atexit.register(commit_on_exit)

    # ---------- 启动二进制 ----------
    binary_path = "/usr/local/bin/qwen2api"
    if not os.path.exists(binary_path):
        raise FileNotFoundError(f"未找到二进制: {binary_path}")

    print("[LOG] 正在拉起 qwen2api 进程...", flush=True)
    process = subprocess.Popen(
        [binary_path],
        env=env,
        cwd="/app",
        stdout=None,
        stderr=None,
    )
    print(f"[LOG] 进程已启动，PID：{process.pid}", flush=True)

    # ---------- 等待端口真正就绪（关键，解决冷启动卡住） ----------
    start_time = time.monotonic()
    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        # 进程提前退出 → 立刻报错
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"qwen2api 启动失败，退出代码：{exit_code}")

        # 端口开始监听
        try:
            with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=1):
                print(f"[LOG] ✅ 服务已就绪，监听端口：{WEB_PORT}", flush=True)
                break
        except OSError:
            time.sleep(0.5)
    else:
        raise RuntimeError("qwen2api 启动超时，请检查容器日志")

    print("==================================================", flush=True)
    print("[LOG] Qwen2API 启动完成，Modal 已接管", flush=True)
    print("==================================================", flush=True)
