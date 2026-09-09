import subprocess
import os

from modal import App, Image, Volume, web_server, Secret, concurrent


########## UTILS FUNCTIONS ##########

def download_hf_model(model_dir: str, model_name: str):
    """Download model from HuggingFace Hub."""
    import os
    from huggingface_hub import snapshot_download

    os.makedirs(model_dir, exist_ok=True)
    hf_token = os.environ.get("HF_TOKEN")  # 公开模型可选

    print(f"Downloading model {model_name} to {model_dir}...")
    snapshot_download(
        repo_id=model_name,
        local_dir=model_dir,
        token=hf_token,
    )
    print(f"Successfully downloaded model {model_name}")


########## CONSTANTS ##########

MODEL_NAME = "Qwen/Qwen3-8B-AWQ"
SERVE_MODEL_NAME = "qwen3-8b-awq"
MODEL_DIR = f"/models/{MODEL_NAME}"

GPU = "T4"

hf_token = os.environ.get("HF_TOKEN")
hf_secret = Secret.from_dict({"HF_TOKEN": hf_token}) if hf_token else None

# 持久化 Volume
model_volume = Volume.from_name(
    f"lmdeploy-model-cache-{SERVE_MODEL_NAME}",
    create_if_missing=True
)


########## IMAGE DEFINITION ##########

image = (
    Image.from_registry("openmmlab/lmdeploy:v0.7.3-cu12")
    .pip_install(
        "huggingface_hub[hf_transfer]>=0.30.0,<1.0",
        "transformers>=4.51.0",
        "grpclib==0.4.7",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)


########## APP SETUP ##########

app = App(f"lmdeploy-{SERVE_MODEL_NAME}")
SERVER_PORT = 23333


@app.function(
    image=image,
    gpu=GPU,
    cpu=2.0,
    memory=16384,
    scaledown_window=200,
    timeout=300,
    max_containers=1,
    volumes={"/models": model_volume},
    secrets=[hf_secret] if hf_secret else [],
)
@concurrent(max_inputs=4)  # T4 跑 8B 建议并发低一点
@web_server(port=SERVER_PORT, startup_timeout=60 * 10)
def serve():
    print("Starting LMDeploy API server...")
    print(f"Model path: {MODEL_DIR}")

    # 检查模型是否已在 Volume 中
    if not os.path.exists(os.path.join(MODEL_DIR, "config.json")):
        print(f"Model not found in volume, downloading...")
        download_hf_model(MODEL_DIR, MODEL_NAME)
        model_volume.commit()
        print("Model downloaded and committed to volume.")
    else:
        print(f"Model already exists in volume, skip download.")

    # 启动命令（AWQ 模型）
    args = [
        "lmdeploy serve api_server",
        f"--model-name {SERVE_MODEL_NAME}",
        f"--server-name 0.0.0.0",
        f"--server-port {SERVER_PORT}",
        "--backend turbomind",
        "--model-format awq",
        "--tp 1",
        "--session-len 16384",             # 真正调整为 16k 标准上下文 window
        "--cache-max-entry-count 0.25",    # 关键点：给 16k KV Cache 精确预留 4GB 显存
        "--max-batch-size 4",              # 限制最大并发数，防止长上下文高并发爆显存
        "--quant-policy 4",                # 启用 KV Cache 4-bit 量化，显存直接省一半！
        MODEL_DIR
    ]

    cmd = " ".join(args)
    print("Running command:", cmd)

    subprocess.Popen(cmd, shell=True)
