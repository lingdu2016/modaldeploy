# 作用：在 Modal 云端使用 llama.cpp (GGUF 引擎) 部署 Qwen3-14B-UD-Q5_K_XL + Gradio 聊天室
# =============================================================================
# 部署前置步骤:
#   本模型 (unsloth/Qwen3-14B-GGUF) 为公开仓库，无需 huggingface-secret 即可下载。
# 部署命令: modal deploy app3.py
# =============================================================================

import os
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
    # 重要: wheel 版本必须与基础镜像的 CUDA 版本匹配 (12.4.1 -> cu124)。
    # 如果这里装的是纯 CPU 版 (或版本不匹配导致装的是 CPU fallback)，
    # n_gpu_layers=-1 设了也不会真的用 GPU，14B 模型跑 CPU 会慢到
    # 只有一两个 token/秒 —— 这是"输出很慢"最常见的真实原因，
    # 比调采样参数重要得多。部署后请务必去看日志确认类似
    # "llm_load_tensors: offloaded 41/41 layers to GPU" 的字样。
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
# S4: 模型服务 (并发锁 + L4 显存安全配置)
# =============================================================================
# @modal.concurrent(max_inputs=20) 只是为了让 Gradio 页面加载所需的多个
# 并行请求 (静态资源/WebSocket) 不互相卡住，不代表允许多路请求同时
# 跑推理——真正的推理仍然靠下面的 asyncio.Lock 强制串行，因为
# llama.cpp 的 context 不是线程安全的，并发调用只会互相拖慢甚至出错。
@app.cls(
    gpu="L4",
    volumes={"/cache": vol},
    scaledown_window=300,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:

    @modal.enter()
    def load_model(self):
        import asyncio
        from llama_cpp import Llama

        # FIX: 之前是 self.lock = None，等于完全没有并发保护。
        # 这里改回真正的 asyncio.Lock，并在 predict() 里实际使用它。
        self.lock = asyncio.Lock()

        model_path = f"/cache/{MODEL_FILE}"

        print("加载 llama.cpp ...")

        self.llm = Llama(
            model_path=model_path,
            # L4 全GPU
            n_gpu_layers=-1,
            # 32k上下文
            n_ctx=32768,
            # 显式设置 batch，避免用到偏保守的默认值
            n_batch=512,
            n_ubatch=512,
            # 开启 flash attention (如果这版 llama-cpp-python 支持编译时启用的话)，
            # 对 L4 这种 Ada 架构通常能明显提速
            flash_attn=True,
            verbose=True,  # 先保持 True，确认下面这行 offload 信息
        )

        print("模型加载完成")

    async def predict(self, message: str, history: list):
        """线程安全、低开销的流式推理逻辑"""
        import asyncio

        async with self.lock:

            # ============================
            # 限制历史长度
            # ============================
            history = history[-10:]

            # ============================
            # 手动拼接 ChatML prompt
            # ============================
            # 不用 create_chat_completion(messages=...) 依赖 GGUF 内嵌
            # jinja 模板自动解析——那一步不稳定，一旦解析异常就会退化
            # 成裸续写，导致模型把用户消息当成故事/脚本开头写下去，
            # 停不下来，表现就是"输出很慢"。这里自己保证格式正确。
            system_prompt = """
你是一个中文AI助手。

规则：

1. 默认使用简体中文回答。
2. 除非用户要求英文，否则不要输出英文。
3. 可以进行：
   - 日常聊天
   - 知识问答
   - 编程帮助
   - 长篇小说创作

小说创作要求：

- 保持人物性格一致。
- 保持世界观连续。
- 不重复已经出现的剧情。
- 注重场景、动作、心理描写。
- 输出正文，不解释写作过程。

回答要自然，不要模拟用户，也不要生成下一轮对话。
"""

            prompt = f"<|im_start|>system\n{system_prompt}<|im_end|>\n"

            for turn in history:
                if isinstance(turn, dict) and isinstance(turn.get("content"), str):
                    prompt += f"<|im_start|>{turn['role']}\n{turn['content']}<|im_end|>\n"

            prompt += f"<|im_start|>user\n{message}<|im_end|>\n"
            # 预填空 think 块，强制关闭思考模式 (Qwen /no_think 的真正实现原理)
            prompt += "<|im_start|>assistant\n<think>\n\n</think>\n\n"

            # ============================
            # 用 asyncio.Queue 代替 queue.Queue + run_in_executor
            # ============================
            # FIX: 之前每收到一个 token 就 await run_in_executor(...)
            # 阻塞等待，相当于每个 token 都提交一次线程池任务。
            # 改成后台线程通过 call_soon_threadsafe 往 asyncio.Queue 里
            # 塞数据，主协程直接 await queue.get()，没有重复的线程池调度开销。
            loop = asyncio.get_event_loop()
            aqueue: asyncio.Queue = asyncio.Queue()
            END = object()

            def worker():
                import time
                token_count = 0
                t_start = time.time()
                t_first_token = None

                try:
                    stream = self.llm.create_completion(
                        prompt=prompt,
                        max_tokens=4096,
                        temperature=0.75,
                        top_p=0.9,
                        top_k=20,
                        min_p=0.05,
                        repeat_penalty=1.18,
                        stream=True,
                        stop=[
                            "<|im_end|>",
                            "<|im_start|>",
                            "<|endoftext|>",
                            "<|eot_id|>",
                            "User:",
                            "用户:",
                        ],
                    )

                    for chunk in stream:
                        text = chunk["choices"][0]["text"]
                        if text:
                            if t_first_token is None:
                                t_first_token = time.time()
                            token_count += 1
                            loop.call_soon_threadsafe(aqueue.put_nowait, text)

                except Exception as e:
                    loop.call_soon_threadsafe(aqueue.put_nowait, e)

                finally:
                    t_end = time.time()
                    if t_first_token is not None and token_count > 0:
                        gen_time = t_end - t_first_token
                        tok_per_sec = token_count / gen_time if gen_time > 0 else 0
                        print(
                            f"⏱️ 首字延迟: {t_first_token - t_start:.2f}s | "
                            f"生成 {token_count} tokens | "
                            f"耗时 {gen_time:.2f}s | "
                            f"速度: {tok_per_sec:.2f} tokens/s"
                        )
                    loop.call_soon_threadsafe(aqueue.put_nowait, END)

            import threading
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            output = ""

            while True:
                item = await aqueue.get()

                if item is END:
                    break

                if isinstance(item, Exception):
                    raise item

                output += item
                yield output

    # =========================================================================
    # Gradio
    # =========================================================================
    @modal.asgi_app()
    def ui(self):
        import gradio as gr
        from fastapi import FastAPI

        web_app = FastAPI()

        async def chat(message, history):
            async for x in self.predict(message, history):
                yield x

        demo = gr.ChatInterface(
            fn=chat,
            type="messages",
            title="Qwen3-14B 中文聊天 + 小说助手",
            description="Modal L4 + llama.cpp",
        )

        # 注意: 这里限制为 1，配合 self.lock 双重保险，
        # 确保任何时候只有一路对话在真正跑推理。
        demo.queue(default_concurrency_limit=1)

        return gr.mount_gradio_app(web_app, demo, path="/")


# =============================================================================
# S5: 本地入口点
# =============================================================================
@app.local_entrypoint()
def main():
    print("部署完成")
    print("modal deploy app3.py")
