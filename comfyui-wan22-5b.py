import os
import subprocess
import time
from pathlib import Path
import modal
import urllib.request
import urllib.error

# ====================== 配置区 ======================
VOLUME_NAME = "wan22-5b-cache"          # 模型缓存卷
OUTPUT_VOLUME_NAME = "wan22-output"   # 生成视频保存卷
APP_NAME = "comfyui-wan22-5b"
GPU_TYPE = "L4"
MAX_CONTAINERS = 1
MAX_INPUTS = 10
TIMEOUT_SECONDS = 1800
SCALEDOWN_WINDOW_SECONDS = 300
WEB_PORT = 8000
WEB_STARTUP_TIMEOUT_SECONDS = 300
HF_SECRET_NAME = "huggingface-secret"
# ====================================================

vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
output_vol = modal.Volume.from_name(OUTPUT_VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .env({"DEBIAN_FRONTEND": "noninteractive"})
    .apt_install(
        "git",
        "nano",
        "libgl1",        
        "libglx-mesa0",  
        "libglib2.0-0",
        "net-tools",  # 新增：用于端口与网络诊断
        "procps",     # 新增：用于进程诊断
    )
    .pip_install(
        "comfy-cli==1.7.1",
        "huggingface-hub==0.36.0",
        "Pillow>=10.0.0",
        "python-dotenv>=1.0.1",
    )
    .run_commands(
        "comfy --skip-prompt install --fast-deps --nvidia --version 0.11.1"
    )
)

# 安装自定义节点
image = (
    image
    .run_commands("comfy node install --fast-deps image-resize-comfyui")
    .run_commands("comfy node install --fast-deps https://github.com/Comfy-Org/ComfyUI-Manager")
    .run_commands("comfy node install --fast-deps https://github.com/ltdrdata/ComfyUI-Impact-Pack")
    .run_commands("comfy node install --fast-deps https://github.com/ltdrdata/ComfyUI-Inspire-Pack")
    .run_commands("comfy node install --fast-deps https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite")
    .run_commands("comfy node install --fast-deps https://github.com/city96/ComfyUI-GGUF")
    .run_commands("comfy node install --fast-deps https://github.com/chrisgoringe/cg-use-everywhere")
    .run_commands("comfy node install --fast-deps https://github.com/Fannovel16/ComfyUI-Frame-Interpolation")
    .run_commands("comfy node install --fast-deps https://github.com/M1kep/ComfyLiterals")
    .run_commands("comfy node install --fast-deps https://github.com/ClownsharkBatwing/RES4LYF")
    .run_commands("comfy node install --fast-deps https://github.com/aria1th/ComfyUI-LogicUtils")
    .run_commands("comfy node install --fast-deps https://github.com/kijai/ComfyUI-KJNodes")
    .run_commands("comfy node install --fast-deps https://github.com/rgthree/rgthree-comfy")
    .run_commands("comfy node install --fast-deps https://github.com/yolain/ComfyUI-Easy-Use")
    .run_commands("comfy node install --fast-deps https://github.com/lihaoyun6/ComfyUI-FlashVSR_Ultra_Fast")
    .run_commands("comfy node install --fast-deps https://github.com/cubiq/ComfyUI_essentials")
    .run_commands("comfy node install --fast-deps https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler")
)


def symlink_into_comfy(
    models_root: Path,
    subdir: str,
    source_path: str | Path,
    target_name: str | None = None,
) -> None:
    source = Path(source_path)
    destination_dir = models_root / subdir
    destination_dir.mkdir(parents=True, exist_ok=True)

    destination = destination_dir / (target_name or source.name)
    if destination.exists() or destination.is_symlink():
        destination.unlink()

    destination.symlink_to(source)


def hf_download_wan22_5b_models():
    """下载 Wan 2.2 5B 相关模型"""
    from huggingface_hub import hf_hub_download

    cache_dir = Path("/cache")
    models_root = Path("/root/comfy/ComfyUI/models")

    model_specs = [
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
            "diffusion_models",
            "wan2.2_ti2v_5B_fp16.safetensors",
        ),
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/vae/wan2.2_vae.safetensors",
            "vae",
            "wan2.2_vae.safetensors",
        ),
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "text_encoders",
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        ),
    ]

    for repo_id, filename, subdir, target_name in model_specs:
        cached_file = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            cache_dir=cache_dir,
        )
        symlink_into_comfy(models_root, subdir, cached_file, target_name)


image = (
    image.env({"HF_XET_HIGH_PERFORMANCE": "1"})
    .run_commands("rm -rf /root/comfy/ComfyUI/output")
    .run_function(
        hf_download_wan22_5b_models,
        volumes={"/cache": vol},
        secrets=[modal.Secret.from_name(HF_SECRET_NAME)],
    )
)

app = modal.App(name=APP_NAME, image=image)


@app.function(
    max_containers=MAX_CONTAINERS,
    gpu=GPU_TYPE,
    volumes={
        "/cache": vol,
        "/root/comfy/ComfyUI/output": output_vol,
    },
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)
@modal.concurrent(max_inputs=MAX_INPUTS)
@modal.web_server(WEB_PORT, startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS)
def ui():
    """启动 ComfyUI Web UI，带完整调试日志记录"""
    print(f"=== [DEBUG] Starting ComfyUI server process on port {WEB_PORT} ===")
    
    # 启动 ComfyUI 并捕捉标准输出与错误
    process = subprocess.Popen(
        f"comfy launch -- --listen 0.0.0.0 --port {WEB_PORT}",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    start_time = time.time()
    check_counter = 0

    while time.time() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        check_counter += 1
        
        # 检查子进程是否已非正常退出
        poll_status = process.poll()
        if poll_status is not None:
            remaining_output, _ = process.communicate()
            print(f"=== [ERROR] ComfyUI process exited unexpectedly with code {poll_status} ===")
            print(f"[PROCESS OUTPUT]:\n{remaining_output}")
            raise RuntimeError(f"ComfyUI exited with code {poll_status}")

        # 尝试通过 HTTP 探测服务就绪状态
        try:
            target_url = f"http://127.0.0.1:{WEB_PORT}"
            req = urllib.request.Request(target_url, headers={"User-Agent": "Modal-Health-Check"})
            
            with urllib.request.urlopen(req, timeout=3) as response:
                print(f"=== [SUCCESS] Health check passed! HTTP Status Code: {response.status} ===")
                print(f"=== [DEBUG] Server response read successfully. Handing over control to Modal proxy. ===")
                # 确定端口正常且响应后，返回成功信号
                return
        except urllib.error.HTTPError as e:
            # 如果能拿到 HTTP 状态码（即使是 404/500），说明 Web 服务器本身已经监听并响应了
            print(f"=== [DEBUG Check #{check_counter}] HTTP Server responded with code {e.code}. Server is alive! ===")
            return
        except Exception as err:
            # 每隔 10 秒打一次诊断日志，防止刷屏
            if check_counter % 5 == 0:
                elapsed = int(time.time() - start_time)
                print(f"[DIAGNOSTIC {elapsed}s] Waiting for HTTP server... Last error: {err}")
                
                # 检查系统 8000 端口占用情况
                try:
                    netstat = subprocess.check_output("netstat -tuln", shell=True, text=True)
                    print(f"[NETSTAT STATE]:\n{netstat.strip()}")
                except Exception as net_err:
                    print(f"[NETSTAT ERROR]: {net_err}")

        time.sleep(2)

    # 超时抛出异常并收集残余输出
    print(f"=== [ERROR] Startup timed out after {WEB_STARTUP_TIMEOUT_SECONDS} seconds ===")
    if process.poll() is None:
        process.terminate()
    out, _ = process.communicate()
    print(f"[FINAL PROCESS LOGS]:\n{out}")
    raise TimeoutError("ComfyUI server failed to become responsive within timeout window.")
