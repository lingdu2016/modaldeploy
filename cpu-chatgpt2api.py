import os
import socket
import subprocess
import threading
import time
import modal

# ====================== 配置区 ======================
APP_NAME = "qwen2api"
VOLUME_NAME = "qwen2api-data"

IMAGE = "rfym21/qwen2api:latest"
WEB_PORT = 8080          # Modal 对外端口（转发端口）
INNER_PORT = 3000        # Qwen 内部监听端口

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 10
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 180

CPU = 0.25
MEMORY_MB = 512

SECRET_NAME = "qwen2api-secret"

# ---------- 环境变量说明 ----------
# API_KEY        【唯一强制必填】管理员密钥（第一个）
# DATA_SAVE_MODE 建议 file（账号持久化到 /app/data）
# 其余（账号、思考开关等）部署后进管理面板配置即可
# ====================================================


vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Alpine 镜像没有 Python，Modal Function 需要 Python，用 apk 装
image = (
    modal.Image.from_registry(IMAGE)
    .dockerfile_commands(
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
    )
)

app = modal.App(name=APP_NAME, image=image)


def _tcp_proxy(listen_port: int, target_host: str, target_port: int):
    """把 0.0.0.0:listen_port 转发到 target_host:target_port（解决只绑 localhost 的问题）"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(128)
    print(f"TCP 代理已启动：0.0.0.0:{listen_port} -> {target_host}:{target_port}", flush=True)

    def _handle(client: socket.socket):
        try:
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.connect((target_host, target_port))
            sockets = [client, upstream]
            while True:
                r, _, _ = select_select(sockets, [], [], 60)
                if not r:
                    break
                for s in r:
                    other = upstream if s is client else client
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                upstream.close()
            except Exception:
                pass

    def select_select(r, w, x, timeout):
        import select
        return select.select(r, w, x, timeout)

    while True:
        try:
            client, _ = server.accept()
            t = threading.Thread(target=_handle, args=(client,), daemon=True)
            t.start()
        except Exception as e:
            print(f"代理 accept 异常: {e}", flush=True)
            time.sleep(0.1)


@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={"/app/data": vol},
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
@modal.web_server(WEB_PORT, startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS)
def ui():
    """启动 Qwen2API + 本地端口转发。"""
    print("======================================", flush=True)
    print("正在启动 Qwen2API...", flush=True)
    print("======================================", flush=True)

    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "API_KEY 未设置。这是官方唯一强制必填项，请在 Modal Secret 中配置。"
        )

    env = os.environ.copy()
    env["API_KEY"] = api_key
    env["SERVICE_PORT"] = str(INNER_PORT)
    # 官方默认就是 localhost，这里不强求改它
    env["DATA_SAVE_MODE"] = os.environ.get("DATA_SAVE_MODE", "file").strip() or "file"

    print(f"API_KEY：已设置", flush=True)
    print(f"DATA_SAVE_MODE：{env['DATA_SAVE_MODE']}", flush=True)
    print(f"内部端口：{INNER_PORT}（localhost）", flush=True)
    print(f"对外端口：{WEB_PORT}（0.0.0.0，经代理转发）", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # 1) 先起官方二进制（听 localhost:3000）
    process = subprocess.Popen(
        ["/usr/local/bin/qwen2api"],
        env=env,
        cwd="/app",
    )
    print(f"Qwen 进程已启动，PID：{process.pid}", flush=True)

    time.sleep(3)
    if process.poll() is not None:
        raise RuntimeError(f"Qwen2API 启动失败，退出代码：{process.poll()}")

    # 2) 再起转发：0.0.0.0:8080 -> 127.0.0.1:3000
    proxy_thread = threading.Thread(
        target=_tcp_proxy,
        args=(WEB_PORT, "127.0.0.1", INNER_PORT),
        daemon=True,
    )
    proxy_thread.start()

    print("进程仍在运行，等待 Web 服务就绪。", flush=True)
