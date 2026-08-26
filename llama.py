# 作用：在 Modal 云端使用 llama.cpp (GGUF 引擎) 部署 Qwen3-14B-UD-Q5_K_XL + Gradio 聊天室
# =============================================================================
# 部署前置步骤:
#   本模型 (unsloth/Qwen3-14B-GGUF) 为公开仓库，无需 huggingface-secret 即可下载。
# 部署命令: modal deploy app2.py
# =============================================================================

import modal

# =============================================================================
# S1: 环境准备 - 基于 CUDA 12.4 镜像，构建包含 llama-cpp-python 与 fastapi 驱动的环境
# =============================================================================
MODEL_REPO = "unsloth/Qwen3-14B-GGUF"
MODEL_FILE = "Qwen3-14B-UD-Q5_K_XL.gguf"

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11"
    )
    .apt_install("git", "wget", "curl")
    .pip_install(
        "fastapi",
        "gradio==5.4.0",
        "huggingface_hub==0.25.2",
        "pydantic<3",
        "requests"
    )
    # FIX: wheel 版本需与基础镜像的 CUDA 版本匹配 (12.4.1 -> cu124)，
    # 否则可能在 @modal.enter() 加载模型阶段因 CUDA 符号不匹配而崩溃，
    # 导致容器起不来、Gradio 页面自然也加载不出来。
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124"
    )
)


# =============================================================================
# S2: 模型预下载 - 从 Hugging Face 持久化缓存 Qwen3-14B GGUF 权重
# =============================================================================
def hf_download():
    """预下载 Qwen3-14B-UD-Q5_K_XL 权重文件至持久化缓存卷中"""
    from huggingface_hub import hf_hub_download

    print(f"📦 开始下载 {MODEL_REPO}/{MODEL_FILE}...")

    hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/cache",
        resume_download=True,
    )

    print("🎉 模型预下载成功完成!")


# =============================================================================
# S3: 持久化卷挂载与 App 初始化
# =============================================================================
vol = modal.Volume.from_name("qwen3-14b-cache", create_if_missing=True)

image = image.run_function(
    hf_download,
    volumes={"/cache": vol},
)

app = modal.App(name="qwen3-14b-gguf-chat-ui-llamacpp", image=image)


# =============================================================================
# S4: Gradio Chat Web 服务 (并发锁 + L4 显存安全配置)
# =============================================================================
# FIX: 加上 @modal.concurrent(max_inputs=10)。
# Modal 容器默认同一时间只处理一个请求，而 Gradio 页面加载需要同时
# 发起多个请求 (HTML / 静态资源 / WebSocket 队列)，不加这个会互相
# 卡住，表现为页面一直转圈加载不出来。
@app.cls(
    gpu="L4",
    volumes={"/cache": vol},
    scaledown_window=300,
    timeout=900,
    max_containers=1,
)
@modal.concurrent(max_inputs=10)
class ModelService:

    @modal.enter()
    def load_model(self):
        """容器启动时加载 llama.cpp 引擎并初始化并发锁"""
        import asyncio
        from llama_cpp import Llama

        # 初始化异步锁，用于串行化推理请求，防止并发调用同一个
        # Llama 实例导致状态错乱或崩溃 (llama.cpp 底层非线程安全)。
        self.lock = asyncio.Lock()

        print("🚀 正在初始化 llama.cpp 引擎 (单卡 L4 优化配置)...")

        self.llm = Llama(
            model_path=f"/cache/{MODEL_FILE}",
            n_gpu_layers=-1,
            # L4 24GB
            n_ctx=16384,
            verbose=False,
        )

        print("✅ 模型加载成功！")

    async def predict(self, message: str, history: list):
        """线程安全的流式推理逻辑"""
        import asyncio

        # 使用异步锁保护 llama.cpp 引擎
        async with self.lock:
            messages = [
                {
                    "role": "system",
                    "content": """
你是一名专业中文小说作者。

要求：

1. 永远使用简体中文。
2. 不输出思考过程。
3. 不输出 <think> 标签。
4. 直接输出最终答案。
5. 支持长篇小说创作。
6. 保持人物和剧情连续。

/no_think
"""
                }
            ]

            for turn in history:
                messages.append({"role": turn["role"], "content": turn["content"]})

            # 追加 /no_think 软开关 (README 中说明的用法)，关闭思考模式
            messages.append({"role": "user", "content": message + "\n/no_think"})

            stream = self.llm.create_chat_completion(
                messages=messages,
                max_tokens=8192,
                temperature=0.7,
                top_p=0.8,
                top_k=20,
                min_p=0,
                repeat_penalty=1.1,
                stream=True,
            )

            response = ""
            for chunk in stream:
                delta = chunk["choices"][0]["delta"]
                text = delta.get("content", "")

                if text:
                    response += text

                    # 防御性清理，防止万一残留 <think>...</think> 泄露到前端
                    cleaned = response
                    if "<think>" in cleaned:
                        cleaned = cleaned.split("</think>")[-1].lstrip("\n")

                    yield cleaned
                    # 主动让出 asyncio 控制权，允许 FastAPI 及时处理心跳包，防止连接中断
                    await asyncio.sleep(0)

    @modal.asgi_app()
    def ui(self):
        """通过 FastAPI 挂载 Gradio 并开启 Server-Sent Events (SSE) 队列"""
        import gradio as gr
        from fastapi import FastAPI

        web_app = FastAPI()

        async def predict_wrapper(message, history):
            async for output in self.predict(message, history):
                yield output

        demo = gr.ChatInterface(
            fn=predict_wrapper,
            type="messages",
            title="Qwen3-14B-GGUF Chatbot (Modal + llama.cpp 单卡 L4 版)",
            description="基于 Modal 云端单卡 L4 与 llama.cpp 引擎部署的流式中文小说创作助手。",
            textbox=gr.Textbox(placeholder="请输入内容...", container=False, scale=7),
        )

        demo.queue(default_concurrency_limit=5)

        return gr.mount_gradio_app(web_app, demo, path="/")


# =============================================================================
# S5: 本地入口点
# =============================================================================
@app.local_entrypoint()
def main():
    print("=" * 60)
    print("Qwen3-14B-GGUF Gradio ChatUI 部署 (llama.cpp 单卡 L4 极速版)")
    print("=" * 60)
    print("📌 部署命令: modal deploy app2.py")
    print("=" * 60)
