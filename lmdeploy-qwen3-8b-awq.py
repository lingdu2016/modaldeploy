import subprocess
import os
from dotenv import load_dotenv

load_dotenv()

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

MODEL_NAME = "Qwen/Qwen3-0.6B"
SERVE_MODEL_NAME = "qwen3-0.6b"
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
        "huggingface_hub[hf_transfer]>=0.30.0,<1.0",  # 解决版本冲突
        "transformers>=4.51.0",                       # Qwen3 需要
        "python-dotenv",
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
    scaledown_window=60,
    timeout=300,
    max_containers=1,
    volumes={"/models": model_volume},
    secrets=[hf_secret] if hf_secret else [],
)
@concurrent(max_inputs=8)
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

    # 启动命令（全精度，不加 --model-format awq）
    args = [
        "lmdeploy serve api_server",
        f"--model-name {SERVE_MODEL_NAME}",
        f"--server-name 0.0.0.0",
        f"--server-port {SERVER_PORT}",
        "--backend turbomind",
        "--session-len 16384",
        "--tp 1",
        "--cache-max-entry-count 0.5",
        MODEL_DIR
    ]

    cmd = " ".join(args)
    print("Running command:", cmd)

    # 与原项目保持一致
    subprocess.Popen(cmd, shell=True)
