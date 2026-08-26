# =============================================================================
# Qwen3.6-14B-A3B-FableVibes-GGUF
# Modal L4 + llama.cpp + Gradio
# 聊天 + 长篇小说生成版
# =============================================================================

import os
import modal

MODEL_REPO = "tvall43/Qwen3.6-14B-A3B-FableVibes-GGUF"
MODEL_FILE = "Qwen3.6-14B-A3B-FableVibes-Q4_K_M.gguf"

# =============================================================================
# 环境
# =============================================================================

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11"
    )
    .apt_install(
        "git",
        "wget",
        "curl"
    )
    .pip_install(
        "fastapi",
        "gradio==5.4.0",
        "huggingface_hub>=0.23.0,<0.26.0",
        "requests",
    )
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu121",
    )
)

# 模型缓存
vol = modal.Volume.from_name(
    "qwen36-14b-cache",
    create_if_missing=True
)

app = modal.App(
    name="qwen36-14b-fable-gradio"
)

# =============================================================================
# 模型服务
# =============================================================================

@app.cls(
    image=image,  # 已添加 image 配置
    gpu="L4",
    volumes={
        "/cache": vol
    },
    scaledown_window=300,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:

    @modal.enter()
    def load_model(self):
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama

        self.lock = None
        model_path = f"/cache/{MODEL_FILE}"

        if not os.path.exists(model_path):
            print(f"下载模型: {MODEL_FILE}")
            hf_hub_download(
                repo_id=MODEL_REPO,
                filename=MODEL_FILE,
                local_dir="/cache"
            )
            vol.commit()

        print("加载 llama.cpp ...")

        self.llm = Llama(
            model_path=model_path,
            # L4 全GPU
            n_gpu_layers=-1,
            # 32k上下文
            n_ctx=32768,
            # 使用GGUF自己的模板
            verbose=False,
        )

        print("模型加载完成")

    async def predict(
        self,
        message,
        history
    ):
        import asyncio
        import queue
        import threading

        # ============================
        # 限制历史长度
        # ============================
        history = history[-10:]

        # ============================
        # system prompt
        # ============================
        messages = [
            {
                "role": "system",
                "content": """
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
            }
        ]

        # ============================
        # 添加历史
        # ============================
        for turn in history:
            if (
                isinstance(turn, dict)
                and isinstance(turn.get("content"), str)
            ):
                messages.append(
                    {
                        "role": turn["role"],
                        "content": turn["content"]
                    }
                )

        messages.append(
            {
                "role": "user",
                "content": message
            }
        )

        result_queue = queue.Queue()
        END = object()

        def worker():
            try:
                response = (
                    self.llm
                    .create_chat_completion(
                        messages=messages,
                        # 聊天+小说平衡
                        max_tokens=4096,
                        temperature=0.75,
                        top_p=0.9,
                        min_p=0.05,
                        repeat_penalty=1.18,
                        stream=True,
                        stop=[
                            "<|im_end|>",
                            "<|endoftext|>",
                            "<|eot_id|>",
                            "User:",
                            "用户:",
                        ]
                    )
                )

                for chunk in response:
                    delta = (
                        chunk["choices"][0]
                        .get("delta", {})
                    )
                    text = delta.get("content")
                    if text:
                        result_queue.put(text)

            except Exception as e:
                result_queue.put(e)
            finally:
                result_queue.put(END)

        thread = threading.Thread(
            target=worker,
            daemon=True
        )
        thread.start()

        loop = asyncio.get_event_loop()
        output = ""

        while True:
            item = await loop.run_in_executor(
                None,
                result_queue.get
            )

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

        async def chat(
            message,
            history
        ):
            async for x in self.predict(
                message,
                history
            ):
                yield x

        demo = gr.ChatInterface(
            fn=chat,
            type="messages",
            title="Qwen3.6-14B 中文聊天 + 小说助手",
            description="Modal L4 + llama.cpp"
        )

        demo.queue(
            default_concurrency_limit=1
        )

        return gr.mount_gradio_app(
            web_app,
            demo,
            path="/"
        )

# =============================================================================
# deploy
# =============================================================================

@app.local_entrypoint()
def main():
    print("部署完成")
    print("modal deploy app.py")
