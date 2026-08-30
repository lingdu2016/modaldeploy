# 作用：在 Modal 云端使用 llama.cpp (GGUF 引擎) 部署 Qwen3-14B-UD-Q5_K_XL
#       并暴露 OpenAI 兼容的 /v1/chat/completions 接口，供外部程序调用
# =============================================================================
# 部署前置步骤:
#   本模型 (unsloth/Qwen3-14B-GGUF) 为公开仓库，无需 huggingface-secret 即可下载。
# 部署命令: modal deploy app3_openai.py
#
# 外部调用示例 (部署完成后，把 URL 换成 modal 分配的 *.modal.run 域名):
#   curl https://<your-app>.modal.run/v1/chat/completions \
#     -H "Content-Type: application/json" \
#     -d '{
#           "model": "qwen3-14b",
#           "messages": [{"role": "user", "content": "你好"}],
#           "stream": true
#         }'
#
# 也可以直接用 openai 官方 SDK (api_key 随便填一个非空字符串即可，
# 因为本服务不做鉴权，SDK 只是要求这个字段非空):
#   from openai import OpenAI
#   client = OpenAI(base_url="https://<your-app>.modal.run/v1", api_key="not-needed")
#   client.chat.completions.create(model="qwen3-14b", messages=[...], stream=True)
#
# 注意: 接口完全公开，任何拿到这个 URL 的人都能调用并消耗你的 GPU 时长。
# 个人写作场景下够用，但如果 URL 泄露出去可能被蹭算力。
# =============================================================================

import time
import uuid
import modal

# =============================================================================
# S1: 环境准备 - 基于 CUDA 12.4 镜像，构建包含 llama-cpp-python 与 fastapi 驱动的环境
# =============================================================================
MODEL_REPO = "unsloth/Qwen3-14B-GGUF"
MODEL_FILE = "Qwen3-14B-UD-Q5_K_XL.gguf"
MODEL_ID = "qwen3-14b"  # 对外暴露的 model 字段名，客户端调用时填这个

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
        "sse-starlette",  # 用于标准 SSE 流式响应
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

app = modal.App(name="qwen3-14b-gguf-openai-api", image=image)


# =============================================================================
# S4: 模型服务 (并发锁 + L4 显存安全配置)
# =============================================================================
# @modal.concurrent(max_inputs=20) 只是为了让多个外部并发 HTTP 请求
# (比如多个客户端同时打进来) 不互相卡在网络层，不代表允许多路请求
# 同时跑推理——真正的推理仍然靠下面的 asyncio.Lock 强制串行，因为
# llama.cpp 的 context 不是线程安全的，并发调用只会互相拖慢甚至出错。
@app.cls(
    gpu="L4",
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
        from llama_cpp import Llama

        self.lock = asyncio.Lock()

        model_path = f"/cache/{MODEL_FILE}"

        print("加载 llama.cpp ...")

        self.llm = Llama(
            model_path=model_path,
            # L4 全GPU
            n_gpu_layers=-1,
            # show-me-the-story 会把角色/世界观/上一章结尾/伏笔/大纲约束
            # 全部注入到写作 prompt 里，实际占用远比普通聊天大，
            # 这里给到 40k 上限 (L4 24G 显存 + Q5_K_XL 量化下大致够用，
            # 如果加载时报显存不足，酌情往下调，比如 32768)。
            n_ctx=40960,
            # 显式设置 batch，避免用到偏保守的默认值
            n_batch=512,
            n_ubatch=512,
            # 开启 flash attention (如果这版 llama-cpp-python 支持编译时启用的话)，
            # 对 L4 这种 Ada 架构通常能明显提速
            flash_attn=True,
            verbose=False,
        )

        print("模型加载完成")

    def _build_prompt(self, messages: list, disable_think: bool = True) -> str:
        """把 OpenAI 风格的 messages 列表拼接成 ChatML prompt。

        不用 create_chat_completion(messages=...) 依赖 GGUF 内嵌
        jinja 模板自动解析——那一步不稳定，一旦解析异常就会退化
        成裸续写，导致模型把用户消息当成故事/脚本开头写下去，
        停不下来，表现就是"输出很慢"。这里自己保证格式正确。
        """
        default_system = """你是一个中文AI助手。

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

回答要自然，不要模拟用户，也不要生成下一轮对话。"""

        has_system = any(m.get("role") == "system" for m in messages)
        prompt = ""

        if not has_system:
            prompt += f"<|im_start|>system\n{default_system}<|im_end|>\n"

        # 只保留最近若干轮，避免 prompt 无限增长
        trimmed = messages[-20:]

        for m in trimmed:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                # 兼容部分客户端把 content 传成多段 part 的情况，只取文本部分
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"

        prompt += "<|im_start|>assistant\n"
        if disable_think:
            # 预填空 think 块，强制关闭思考模式 (Qwen /no_think 的真正实现原理)
            prompt += "<think>\n\n</think>\n\n"

        return prompt

    async def generate_stream(self, messages: list, temperature: float, top_p: float,
                               max_tokens: int, disable_think: bool = True):
        """线程安全、低开销的流式推理逻辑，逐 token yield 增量文本 (delta)"""
        import asyncio

        async with self.lock:
            prompt = self._build_prompt(messages, disable_think=disable_think)

            # 用 asyncio.Queue 代替 queue.Queue + run_in_executor：
            # 后台线程通过 call_soon_threadsafe 往 asyncio.Queue 里塞数据，
            # 主协程直接 await queue.get()，没有重复的线程池调度开销。
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
                        top_k=20,
                        min_p=0.05,
                        repeat_penalty=1.18,
                        stream=True,
                        # 只保留真正的 ChatML 控制符做 stop，不再加
                        # "User:"/"用户:" —— show-me-the-story 生成的是小说
                        # 正文，角色对话里完全可能出现这类称呼开头的台词，
                        # 用它们当 stop word 会把正文腰斩。
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

        # 不鉴权：任何人拿到这个 URL 都能直接调用。个人写作场景下最省事，
        # 但意味着接口完全公开——如果不希望被陌生人蹭算力，之后可以
        # 随时加回 Authorization 校验。
        web_app = FastAPI(title="Qwen3-14B OpenAI-compatible API")

        class ChatMessage(BaseModel):
            role: str
            content: Union[str, list]

        class ChatCompletionRequest(BaseModel):
            model: str = MODEL_ID
            messages: List[ChatMessage]
            temperature: Optional[float] = 0.75
            top_p: Optional[float] = 0.9
            # show-me-the-story 单章目标字数可能到几千字，中文一个字约
            # 1.5~2 token，8192 常常不够写完一整章，默认调大一些；
            # 它自己没传 max_tokens 时就用这个默认值。
            max_tokens: Optional[int] = 16384
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
                    # 首个 chunk 带 role
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
                # 非流式：内部仍然用流式生成，只是攒完整再一次性返回
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
    print("接口不做鉴权，任何人拿到 URL 都能直接调用")
