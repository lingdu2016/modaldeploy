# =============================================================================
# Modal LMDeploy (OpenAI API 兼容) 无 Secret / 免 Token 极速部署脚本
# =============================================================================

import os
import subprocess
import modal

# =============================================================================
# S1: 常量定义与配置
# =============================================================================
MODEL_NAME = "Qwen/Qwen3-0.6B"
SERVE_MODEL_NAME = "qwen3-0.6b"
MODEL_DIR = f"/models/{MODEL_NAME}"
SERVER_PORT = 23333
GPU_TYPE = "T4"

# 定义持久化 Volume
model_volume = modal.Volume.from_name(
    f"lmdeploy-cache-{SERVE_MODEL_NAME}", 
    create_if_missing=True
)


# =============================================================================
# S2: 镜像构建 (基于 LMDeploy 官方 CUDA 镜像)
# =============================================================================
image = (
    modal.Image.from_registry(
        "openmmlab/lmdeploy:v0.7.3-cu12",
    )
    .pip_install(
        "huggingface_hub==0.25.2",
        "python-dotenv",
        "grpclib==0.4.7",
    )
)


# =============================================================================
# S3: 预下载模型逻辑 (借鉴 app3.py: 构建期完成下载与 Volume 持久化)
# =============================================================================
def download_model_to_volume():
    """在 Modal Image Build 阶段将公开模型下载至 Volume 中，避免运行时冷启动卡顿"""
    from huggingface_hub import snapshot_download

    print(f"📦 [Build Step] 开始下载公开模型: {MODEL_NAME} -> {MODEL_DIR}...")
    os.makedirs(MODEL_DIR, exist_ok=True)

    try:
        snapshot_download(
            repo_id=MODEL_NAME,
            local_dir=MODEL_DIR,
            # 公开仓库无需 token
        )
        print(f"🎉 [Build Step] 模型 {MODEL_NAME} 下载成功完成！")
    except Exception as e:
        print(f"❌ [Build Step] 模型下载失败: {e}")
        raise e


# 将预下载步骤嵌入镜像构建过程，绑定持久化卷
image = image.run_function(
    download_model_to_volume,
    volumes={"/models": model_volume},
)

# 初始化 Modal App
app = modal.App(name=f"lmdeploy-{SERVE_MODEL_NAME}", image=image)


# =============================================================================
# S4: LMDeploy Web 服务 (配置极速健康检查与详细日志输出)
# =============================================================================
@app.function(
    gpu=GPU_TYPE,
    cpu=2.0,
    memory=16384,                            # T4 16GB 内存配额
    scaledown_window=150,                     # 延长缩容等待，减少重复冷启动
    timeout=600,
    max_containers=1,
    volumes={"/models": model_volume},
)
@modal.concurrent(max_inputs=8)
@modal.web_server(
    port=SERVER_PORT,
    startup_timeout=300,
    # 显式指定 OpenAPI/FastAPI 的标准就绪探测接口，探测通过后瞬间放行流量 (1 live)
    startup_check=modal.web_server.HTTPCheck("/v1/models")
)
def serve():
    """启动 LMDeploy API 服务，暴露 OpenAI 兼容接口"""
    print("\n" + "="*60)
    print("🚀 正在启动 LMDeploy OpenAI API Server...")
    print(f"📌 模型路径: {MODEL_DIR}")
    print(f"📌 服务名称: {SERVE_MODEL_NAME}")
    print(f"📌 监听端口: {SERVER_PORT}")
    print("="*60 + "\n")

    # 校验模型文件完整性
    config_path = os.path.join(MODEL_DIR, "config.json")
    if os.path.exists(config_path):
        print(f"✅ 模型文件校验通过 (找到 {config_path})")
    else:
        print(f"⚠️ 警告: 未在 {MODEL_DIR} 找到 config.json，尝试检查目录文件:")
        if os.path.exists(MODEL_DIR):
            print("目录内容:", os.listdir(MODEL_DIR))
        else:
            print(f"❌ 错误: 路径 {MODEL_DIR} 不存在！")

    # 组装 LMDeploy 启动命令
    cmd = [
        "lmdeploy", "serve", "api_server",
        MODEL_DIR,
        "--model-name", SERVE_MODEL_NAME,
        "--server-name", "0.0.0.0",          # 必须绑定 0.0.0.0 才能穿透 Modal 网络
        "--server-port", str(SERVER_PORT),
        "--backend", "turbomind",
        "--session-len", "16384",
        "--tp", "1",
        "--cache-max-entry-count", "0.3",    # 专为 T4 显存优化的 Safe Ratio
    ]

    print("🔧 执行启动命令:")
    print(" ".join(cmd))
    print("\n⏳ 正在加载 C++ TurboMind 引擎与显存分配，请稍候...\n")

    # 使用 Popen + 实时日志管道输出，确保终端能即时打出 LMDeploy 内部 C++ 与 Python 日志
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    # 循环读取并刷新输出日志
    if process.stdout:
        for line in iter(process.stdout.readline, ''):
            print(line, end='', flush=True)

    process.wait()


# =============================================================================
# S5: 本地入口点
# =============================================================================
@app.local_entrypoint()
def main():
    print("✨ 部署配置加载完毕！请执行以下命令进行部署：")
    print("👉 modal deploy <your_script_name>.py")
