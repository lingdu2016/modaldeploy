import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "qwen2api"                                # Modal App 名称
VOLUME_NAME = "qwen2api-data"                        # 持久化数据卷名称

# 镜像配置（官方 Docker Hub 镜像，Alpine 单静态二进制，约 46MB）
IMAGE = "rfym21/qwen2api:latest"
WEB_PORT = 3000                                      # 官方默认 SERVICE_PORT=3000

MAX_CONTAINERS = 1                                   # 自用最多 1 个容器
MAX_CONCURRENT_INPUTS = 8                            # 单容器并发输入
TIMEOUT_SECONDS = 3600                               # 单次函数最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 300                       # 空闲后缩容等待
WEB_STARTUP_TIMEOUT_SECONDS = 180                    # Web 启动等待

# 资源（官方镜像很轻，自用可低配）
CPU = 0.25                                           # 0.25 核足够
MEMORY_MB = 512                                      # 512MB 足够，紧张再提到 768

# Modal Secret 名称（必须提前创建）
SECRET_NAME = "qwen2api-secret"
# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像
# ====================================================
image = (
    modal.Image.from_registry(IMAGE)
    .dockerfile_commands(
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
    )
)

app = modal.App(
    name=APP_NAME,
    image=image,
)

# ====================================================
# 3. 启动 qwen2api
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={"/app/data": vol},                        # 官方数据目录
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
    """启动 Qwen2API（官方二进制入口）并输出详细日志。"""
    print("==================================================", flush=True)
    print("[LOG] 🚀 正在初始化 Qwen2API 启动流程...", flush=True)
    print("==================================================", flush=True)

    # ------------------------------------------------
    # 3.1 环境变量校验与日志记录
    # ------------------------------------------------
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ❌ 致命错误：API_KEY 未设置！", flush=True)
        raise ValueError(
            "API_KEY 未设置。"
            "这是官方唯一强制必填项，请在 Modal Secret 中配置。"
            "第一个密钥为管理员，可登录管理面板。"
        )
    print("[LOG] ✅ 成功获取环境变量: API_KEY (长度: %d 位)" % len(api_key), flush=True)

    # 打印其他可选 Secret 状态（不打印明文）
    optional_secrets = ["ACCOUNTS", "OUTPUT_THINK", "SEARCH_INFO_MODE", "CACHE_MODE"]
    for sec in optional_secrets:
        val = os.environ.get(sec, "").strip()
        if val:
            print(f"[LOG] ℹ️ 检测到自定义 Secret -> {sec}: 已配置", flush=True)
        else:
            print(f"[LOG] ℹ️ 检测到自定义 Secret -> {sec}: 未配置（将使用程序默认值）", flush=True)

    # ------------------------------------------------
    # 3.2 组装环境变量 + 写入 .env
    # ------------------------------------------------
    env = os.environ.copy()

    listen_address = "0.0.0.0"                        # 容器内必须 0.0.0.0
    service_port = str(WEB_PORT)
    data_save_mode = os.environ.get("DATA_SAVE_MODE", "none").strip() or "none"

    env["API_KEY"] = api_key
    env["LISTEN_ADDRESS"] = listen_address
    env["SERVICE_PORT"] = service_port
    env["DATA_SAVE_MODE"] = data_save_mode

    env_file = "/app/.env"
    print(f"[LOG] 📝 正在向 {env_file} 写入运行时配置...", flush=True)
    try:
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"API_KEY={api_key}\n")
            f.write(f"LISTEN_ADDRESS={listen_address}\n")
            f.write(f"SERVICE_PORT={service_port}\n")
            f.write(f"DATA_SAVE_MODE={data_save_mode}\n")
        print(f"[LOG] ✅ 成功写入 {env_file}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 写入 {env_file} 失败: {e}", flush=True)
        raise e

    # ------------------------------------------------
    # 3.3 详细启动参数概览
    # ------------------------------------------------
    print("--------------------------------------------------", flush=True)
    print(f"[LOG] 📊 容器资源与网络配置摘要：", flush=True)
    print(f"       - 监听地址 (LISTEN_ADDRESS) : {listen_address}", flush=True)
    print(f"       - 服务端口 (SERVICE_PORT)   : {service_port}", flush=True)
    print(f"       - 数据持久化模式             : {data_save_mode}", flush=True)
    print(f"       - 数据挂载目录               : /app/data", flush=True)
    print(f"       - 容器资源分配               : CPU {CPU} / 内存 {MEMORY_MB} MB", flush=True)
    print("--------------------------------------------------", flush=True)

    # ------------------------------------------------
    # 3.4 启动二进制子进程
    # ------------------------------------------------
    binary_path = "/usr/local/bin/qwen2api"
    print(f"[LOG] ⚙️ 准备拉起核心二进制进程: {binary_path}", flush=True)
    
    # 检查二进制是否存在
    if not os.path.exists(binary_path):
        print(f"[ERROR] ❌ 致命错误：在路径 {binary_path} 未找到 qwen2api 可执行文件！", flush=True)
    
    try:
        process = subprocess.Popen(
            [binary_path],
            env=env,
            cwd="/app",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        print(f"[LOG] ✅ 二进程已成功拉起，进程 PID：{process.pid}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 启动子进程发生异常: {e}", flush=True)
        raise e

    # ------------------------------------------------
    # 3.5 探针检查与实时日志捕获
    # ------------------------------------------------
    print(f"[LOG] ⏳ 正在进行启动健康检查（等待 3 秒）...", flush=True)
    
    # 循环检查前 3 秒是否有过早崩溃输出
    start_wait = time.time()
    while time.time() - start_wait < 3:
        ret = process.poll()
        if ret is not None:
            # 如果进程提前退出了，把标准输出全打出来方便排查
            output, _ = process.communicate()
            print(f"[ERROR] ❌ qwen2api 进程异常退出，退出码：{ret}", flush=True)
            print(f"[ERROR] 📄 崩溃日志输出:\n{output}", flush=True)
            raise RuntimeError(f"Qwen2API 启动失败，退出代码：{ret}")
        time.sleep(0.5)

    print("[LOG] 🎉 进程运行稳定，未检测到早期崩溃。Modal Web 代理已接管端口 3000。", flush=True)
    print("==================================================", flush=True)
