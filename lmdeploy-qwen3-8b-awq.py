# 第 1 行注释：适用于 Modal T4 GPU 的无 Secret/免 Token LMDeploy OpenAI API 部署脚本
import subprocess
import os
from modal import App, Image, Volume, web_server, concurrent

########## UTILS FUNCTIONS ##########

def download_hf_model(model_dir: str, model_name: str):
    """从 Hugging Face 公开仓库下载模型至 Modal Volume 存储路径中"""
    import os
    from huggingface_hub import snapshot_download

    os.makedirs(model_dir, exist_ok=True)

    try:
        print(f"Downloading public model {model_name} to {model_dir}...")
        snapshot_download(
            repo_id=model_name,
            local_dir=model_dir,
            # 公开模型无需提供 token
        )
        print(f"Successfully downloaded model {model_name}")
    except Exception as e:
        print(f"Error downloading model: {e}")
        raise


########## CONSTANTS ##########

# 当前测试用公开模型（后续更换为 AWQ 量化模型时同步修改此处）
MODEL_NAME = "Qwen/Qwen3-0.6B"
SERVE_MODEL_NAME = "qwen3-0.6b"

MODEL_DIR = f"/models/{MODEL_NAME}"
GPU = "T4"

# 持久化 Volume 存储
model_volume = Volume.from_name(
    f"lmdeploy-model-cache-{SERVE_MODEL_NAME}",
    create_if_missing=True
)


########## IMAGE DEFINITION ##########

image = (
    Image.from_registry(
        "openmmlab/lmdeploy:v0.7.3-cu12",
    )
    .pip_install(
        "huggingface_hub",
        "python-dotenv",
        "grpclib==0.4.7",
    )
)


########## APP SETUP ##########

app = App(f"lmdeploy-{SERVE_MODEL_NAME}")

SERVER_PORT = 23333


@app.function(
    image=image,
    gpu=GPU,
    cpu=2.0,
    memory=16384,                            # T4 16GB 内存配额
    scaledown_window=60,
    timeout=600,
    max_containers=1,
    volumes={
        "/models": model_volume,            # 把 Volume 挂载到 /models
    },
)
@concurrent(max_inputs=8)
@web_server(port=SERVER_PORT, startup_timeout=60 * 8)
def serve():
    """启动 LMDeploy API 服务，暴露 OpenAI 兼容接口"""
    print("Starting LMDeploy API server...")

    # 检查模型是否已存在于挂载的 Volume 中
    if not os.path.exists(os.path.join(MODEL_DIR, "config.json")):
        print(f"Model not found in volume, downloading to {MODEL_DIR}...")
        download_hf_model(MODEL_DIR, MODEL_NAME)
        model_volume.commit()               # 提交下载内容至持久化卷
        print("Model downloaded and committed to volume.")
    else:
        print(f"Model already exists in volume at {MODEL_DIR}, skip download.")

    args = [
        "lmdeploy", "serve", "api_server",
        MODEL_DIR,
        "--model-name", SERVE_MODEL_NAME,
        "--server-name", "0.0.0.0",          # 必须绑定 0.0.0.0 才能透传 Modal 网络
        "--server-port", str(SERVER_PORT),
        "--backend", "turbomind",
        "--session-len", "16384",
        "--tp", "1",
        "--cache-max-entry-count", "0.3",    # 专为 T4 显存优化的安全比例
        # "--model-format", "awq",           # 若后续换为 AWQ 量化模型，取消此行注释
    ]

    print("Running command:", " ".join(args))

    # 使用 subprocess.run 保持主进程阻塞，避免 Modal 容器提前退出
    subprocess.run(args)
