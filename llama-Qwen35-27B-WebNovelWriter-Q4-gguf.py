# 作用：在 Modal L4 (24GB 显存) 上部署 Qwen3.5-27B-WebNovel-Writer-zh-GGUF
#      开启 Q8 KV Cache，将上下文极限拉大至 32768 (32k)
# =============================================================================
# 部署命令: modal deploy app3_openai.py
# =============================================================================

import time
import uuid
import modal

# =============================================================================
# S1: 环境准备 - 基于 CUDA 12.4 镜像
# =============================================================================
MODEL_REPO = "wcn123/Qwen3.5-27B-WebNovel-Writer-zh-GGUF"
MODEL_FILE = "Qwen3.5-27B-WebNovel-Writer-zh-Q4_K_M.gguf"
MODEL_ID = "qwen3.5-27b-webnovel"  # 客户端调用填这个

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11"
    )
    .apt_install("git", "wget", "curl")
    .pip_install(
        "fastapi",
        "huggingface_hub==0.25.2",
        "pydantic<3",
        "requests",
        "sse-starlette",
    )
    # CUDA 12.4 对应的 llama-cpp-python whl
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124"
    )
)


# =============================================================================
# S2: 模型预下载
# =============================================================================
def hf_download():
    """预下载 Qwen3.5-27B-WebNovel-Writer-zh-Q4_K_M 权重文件至持久化缓存卷"""
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
vol = modal.Volume.from_name("qwen3.5-27b-cache", create_if_missing=True)

image = image.run_function(
    hf_download,
    volumes={"/cache": vol},
)

app = modal.App(name="qwen3.5-27b-gguf-openai-api", image=image)


# =============================================================================
# S4: 模型服务 (严格指定 L4 + KV Cache 量化 + 32k 上下文)
# =============================================================================
@app.cls(
    gpu="L4",  # 强行指定 L4 GPU (24GB VRAM)
    volumes={"/cache": vol},
    scaledown_window=150,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:

    @modal.enter()
    def load_model(self):
        import asyncio
        from llama_cpp import Llama, GGML_TYPE_Q8_0

        self.lock = asyncio.Lock()

        model_path = f"/cache/{MODEL_FILE}"

        print("加载 llama.cpp (开启 Q8 KV Cache 优化以支撑 L4 32k 上下文)...")

        self.llm = Llama(
            model_path=model_path,
            n_gpu_layers=-1,      # 全部层 offload 到 L4 GPU
            n_ctx=24576,          # L4 下优化后的极限上下文 (32k token)
            n_batch=512,          # 处理 prompt 时的批次大小
            n_ubatch=512,
            flash_attn=True,      # 必须开启 Flash Attention 以提升长文本速度并降低显存开销
            # 关键优化：将 Key/Value Cache 量化为 8-bit (Q8_0)，显存开销降低 ~50%
            type_k=GGML_TYPE_Q8_0,
            type_v=GGML_TYPE_Q8_0,
            verbose=False,
        )

        print("模型加载完成，当前可用最大上下文：32768")

    def _build_prompt(self, messages: list) -> str:
        """把 OpenAI 风格的 messages 列表拼接成 ChatML prompt"""
        default_system = """你是一位中文网文小说写作助手，
擅长创作高质量小说正文。

要求：

1. 使用简体中文。
2. 直接输出小说正文。
3. 不解释写作过程。
4. 保持人物性格一致。
5. 保持世界观连续。
6. 保留已有设定。
7. 注重环境描写、动作描写、心理描写。
8. 避免AI式总结。"""

        has_system = any(m.get("role") == "system" for m in messages)
        prompt = ""

        if not has_system:
            prompt += f"<|im_start|>system\n{default_system}<|im_end|>\n"

        # 32k 极大上下文下，可以适当多保留历史对话（如最近 40 轮），方便小说连贯创作
        trimmed = messages[-40:]

        for m in trimmed:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"

        prompt += "<|im_start|>assistant\n"
        return prompt

    async def generate_stream(self, messages: list, temperature: float, top_p: float,
                               max_tokens: int):
        """线程安全、低开销的流式推理逻辑"""
        import asyncio

        async with self.lock:
            prompt = self._build_prompt(messages)

            loop = asyncio.get_event_loop()
            aqueue: asyncio.Queue = asyncio.Queue()
            END = object()

            def worker():
                import time as _time
                token_count = 0
                t_start = _time.time()
                t_first_token = None

                try:
                    stream = self.llm.create_completion(
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        top_p=top_p,
                        top_k=40,
                        min_p=0.05,
                        repeat_penalty=1.1,  # 网文创作推荐 1.08~1.12，避免重复但又不破坏文笔
                        stream=True,
                        stop=[
                            "<|im_end|>",
                            "<|im_start|>",
                            "<|endoftext|>",
                            "<|eot_id|>",
                        ],
                    )

                    for chunk in stream:
                        text = chunk["choices"][0]["text"]
                        if text:
                            if t_first_token is None:
                                t_first_token = _time.time()
                            token_count += 1
                            loop.call_soon_threadsafe(aqueue.put_nowait, text)

                except Exception as e:
                    loop.call_soon_threadsafe(aqueue.put_nowait, e)

                finally:
                    t_end = _time.time()
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

            while True:
                item = await aqueue.get()
                if item is END:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item

    # =========================================================================
    # OpenAI 兼容 API
    # =========================================================================
    @modal.asgi_app()
    def api(self):
        import json
        from fastapi import FastAPI, Request
        from fastapi.responses import StreamingResponse
        from pydantic import BaseModel
        from typing import Optional, List, Union

        web_app = FastAPI(title="Qwen3.5-27B WebNovel Writer OpenAI-compatible API")

        class ChatMessage(BaseModel):
            role: str
            content: Union[str, list]

        class ChatCompletionRequest(BaseModel):
            model: str = MODEL_ID
            messages: List[ChatMessage]
            temperature: Optional[float] = 0.75
            top_p: Optional[float] = 0.9
            max_tokens: Optional[int] = 8192  # 支持单次写较长的大章节输出
            stream: Optional[bool] = False

        @web_app.get("/v1/models")
        async def list_models(request: Request):
            return {
                "object": "list",
                "data": [
                    {
                        "id": MODEL_ID,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "self-hosted",
                    }
                ],
            }

        @web_app.post("/v1/chat/completions")
        async def chat_completions(req: ChatCompletionRequest, request: Request):
            messages = [m.model_dump() for m in req.messages]
            completion_id = f"chatcmpl-{uuid.uuid4().hex}"
            created = int(time.time())

            if req.stream:
                async def event_generator():
                    first_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                        ],
                    }
                    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

                    async for delta_text in self.generate_stream(
                        messages, req.temperature, req.top_p, req.max_tokens
                    ):
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": req.model,
                            "choices": [
                                {"index": 0, "delta": {"content": delta_text}, "finish_reason": None}
                            ],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    final_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ],
                    }
                    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    event_generator(), media_type="text/event-stream"
                )

            else:
                full_text = ""
                async for delta_text in self.generate_stream(
                    messages, req.temperature, req.top_p, req.max_tokens
                ):
                    full_text += delta_text

                return {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": full_text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }

        @web_app.get("/health")
        async def health():
            return {"status": "ok"}

        return web_app


# =============================================================================
# S5: 本地入口点
# =============================================================================
@app.local_entrypoint()
def main():
    print("部署完成")
    print("modal deploy app3_openai.py")
