# =============================================================================
# Modal + LMDeploy (Qwen3-8B-AWQ) 稳定部署脚本
# =============================================================================

import os
import subprocess
import modal

# =============================================================================
# S1: 配置与常量
# =============================================================================
MODEL_NAME = "Qwen/Qwen3-8B-AWQ"            # Hugging Face 模型 ID
SERVE_MODEL_NAME = "qwen3-8b-awq"           # 服务暴露的模型名称
MODEL_DIR = f"/models/{MODEL_NAME}"
SERVER_PORT = 23333
GPU_TYPE = "T4"                             # 根据需求选择: T4, L4, A10G 等

# 定义持久化 Volume，用于缓存模型权重
model_volume = modal.Volume.from_name(
    f"lmdeploy-cache-{SERVE_MODEL_NAME}", 
    create_if_missing=True
)

# =============================================================================
# S2: 镜像定义与预下载
# =============================================================================
image = (
    modal.Image.from_registry("openmmlab/lmdeploy:v0.7.3-cu12")
    .pip_install(
        "huggingface_hub==0.25.2",
        "python-dotenv",
        "grpclib==0.4.7"
    )
)

def download_model_to_volume():
    """在 Modal 构建镜像期下载模型，彻底消除运行时的下载等待时间"""
    from huggingface_hub import snapshot_download

    print(f"📦 [Build Step] 开始预下载模型: {MODEL_NAME} -> {MODEL_DIR}...")
    os.makedirs(MODEL_DIR, exist_ok=True)

    snapshot_download(
        repo_id=MODEL_NAME,
        local_dir=MODEL_DIR,
        resume_download=True,
    )
    print(f"🎉 [Build Step] 模型 {MODEL_NAME} 预下载完成！")

# 将下载动作绑定到镜像构建阶段
image = image.run_function(
    download_model_to_volume,
    volumes={"/models": model_volume},
)

app = modal.App(name=f"lmdeploy-{SERVE_MODEL_NAME}", image=image)

# =============================================================================
# S3: 服务核心逻辑 (带详细日志输出)
# =============================================================================
@app.function(
    gpu=GPU_TYPE,
    cpu=2.0,
    memory=16384,
    scaledown_window=150,
    timeout=600,
    max_containers=1,
    volumes={"/models": model_volume},
)
@modal.concurrent(max_inputs=8)
@modal.web_server(
    port=SERVER_PORT,
    startup_timeout=300  # 给予 300 秒的启动与显存加载缓冲，TCP 连通即就绪
)
def serve():
    """启动 LMDeploy API Server 并实时输出日志"""
    print("\n" + "="*60)
    print("🚀 正在启动 LMDeploy API 服务...")
    print(f"📌 模型路径: {MODEL_DIR}")
    print(f"📌 监听端口: {SERVER_PORT}")
    print("="*60 + "\n", flush=True)

    # 1. 检查模型权重
    config_file = os.path.join(MODEL_DIR, "config.json")
    if os.path.exists(config_file):
        print(f"✅ 模型文件完整性校验通过 (找到 config.json)", flush=True)
    else:
        print(f"⚠️ 警告: 未找到 {config_file}，请检查 Volume 挂载", flush=True)

    # 2. 构造启动命令
    cmd = [
        "lmdeploy", "serve", "api_server",
        MODEL_DIR,
        "--model-name", SERVE_MODEL_NAME,
        "--server-name", "0.0.0.0",
        "--server-port", str(SERVER_PORT),
        "--backend", "turbomind",
        "--model-format", "awq",               # 量化模型显式指定 awq 格式
        "--session-len", "16384",
        "--tp", "1",
        "--cache-max-entry-count", "0.3",
    ]

    print(f"🔧 执行启动指令: {' '.join(cmd)}\n", flush=True)

    # 3. 管道化实时日志输出
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    # 持续刷新输出子进程日志到 Modal 控制台
    if process.stdout:
        for line in iter(process.stdout.readline, ''):
            print(line, end='', flush=True)

    process.wait()

# =============================================================================
# S4: 本地入口
# =============================================================================
@app.local_entrypoint()
def main():
    print("✨ 配置正常，执行 'modal deploy <script_name>.py' 部署服务")
