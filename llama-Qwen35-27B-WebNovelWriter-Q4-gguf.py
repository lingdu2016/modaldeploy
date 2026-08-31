# =============================================================================
# Modal 部署 Qwen3.5-27B-WebNovel-Writer-Q4_K_M GGUF
# llama.cpp + OpenAI Compatible API
#
# 部署:
# modal deploy qwen35_webnovel_l4.py
#
# API:
# /v1/chat/completions
# =============================================================================

import time
import uuid
import modal


# =============================================================================
# 模型配置
# =============================================================================

MODEL_REPO = "wcn123/Qwen3.5-27B-WebNovel-Writer-zh-GGUF"

MODEL_FILE = (
    "Qwen3.5-27B-WebNovel-Writer-zh-Q4_K_M.gguf"
)

MODEL_ID = "qwen3.5-27b-webnovel"


# =============================================================================
# 镜像
# =============================================================================

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11",
    )
    .apt_install(
        "git",
        "wget",
        "curl",
    )
    .pip_install(
        "fastapi",
        "huggingface_hub==0.25.2",
        "pydantic<3",
        "requests",
        "sse-starlette",
    )
    .pip_install(
        "llama-cpp-python==0.3.35",
        extra_index_url=
        "https://abetlen.github.io/llama-cpp-python/whl/cu124",
    )
)


# =============================================================================
# 模型缓存
# =============================================================================

volume = modal.Volume.from_name(
    "qwen35-webnovel-cache",
    create_if_missing=True,
)


# =============================================================================
# 下载模型
# =============================================================================

def download_model():

    from huggingface_hub import hf_hub_download

    print("开始下载模型")

    hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/cache",
        resume_download=True,
    )

    print("模型下载完成")


image = image.run_function(
    download_model,
    volumes={
        "/cache": volume
    },
)


# =============================================================================
# APP
# =============================================================================

app = modal.App(
    name="qwen35-webnovel-openai-api",
    image=image,
)



# =============================================================================
# 服务
# =============================================================================


@app.cls(
    gpu="L4",
    volumes={
        "/cache": volume
    },
    timeout=600,
    scaledown_window=300,
    max_containers=1,
)
class ModelService:


    @modal.enter()
    def load_model(self):

        import asyncio

        from llama_cpp import (
            Llama,
            GGML_TYPE_Q8_0,
        )


        self.lock = asyncio.Lock()


        model_path = (
            f"/cache/{MODEL_FILE}"
        )


        print(
            "加载 Qwen3.5-27B WebNovel..."
        )


        self.llm = Llama(

            model_path=model_path,


            # 全部 GPU
            n_gpu_layers=-1,


            # L4稳定配置
            n_ctx=24576,


            # batch
            n_batch=512,
            n_ubatch=512,


            # Flash Attention
            flash_attn=True,


            # Q8 KV Cache
            type_k=GGML_TYPE_Q8_0,
            type_v=GGML_TYPE_Q8_0,


            verbose=False,
        )


        print(
            "模型加载完成"
        )



    # =============================================================
    # 小说 Prompt
    # =============================================================

    def build_prompt(
        self,
        messages
    ):

        system_prompt = """

你是一位中文网文小说写作助手，
擅长创作高质量小说正文。

要求：

1. 使用简体中文。
2. 直接输出小说正文。
3. 不解释写作过程。
4. 保持人物性格一致。
5. 保持世界观连续。
6. 保留已有设定。
7. 注重环境描写、动作描写、心理描写。
8. 避免AI式总结。

"""


        prompt = ""


        has_system = any(
            x.get("role") == "system"
            for x in messages
        )


        if not has_system:

            prompt += (
                "<|im_start|>system\n"
                + system_prompt
                + "<|im_end|>\n"
            )


        # 保留最近40轮
        messages = messages[-40:]


        for m in messages:

            role = m.get(
                "role",
                "user"
            )

            content = m.get(
                "content",
                ""
            )


            prompt += (
                f"<|im_start|>{role}\n"
                f"{content}"
                "<|im_end|>\n"
            )


        prompt += (
            "<|im_start|>assistant\n"
        )


        return prompt



    # =============================================================
    # 流式生成
    # =============================================================

    async def generate(
        self,
        messages,
        temperature,
        top_p,
        max_tokens,
    ):

        import asyncio
        import threading


        async with self.lock:


            prompt = self.build_prompt(
                messages
            )


            loop = asyncio.get_event_loop()

            queue = asyncio.Queue()

            END = object()



            def worker():

                try:


                    stream = self.llm.create_completion(

                        prompt=prompt,

                        max_tokens=max_tokens,


                        temperature=temperature,


                        top_p=top_p,


                        top_k=40,


                        repeat_penalty=1.1,


                        stream=True,


                        stop=[
                            "<|im_end|>",
                            "<|im_start|>",
                            "<|endoftext|>",
                        ],

                    )


                    for chunk in stream:


                        text = (
                            chunk["choices"][0]
                            ["text"]
                        )


                        if text:

                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                text,
                            )


                except Exception as e:


                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        e,
                    )


                finally:


                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        END,
                    )



            threading.Thread(
                target=worker,
                daemon=True,
            ).start()



            while True:

                item = await queue.get()


                if item is END:

                    break


                if isinstance(
                    item,
                    Exception
                ):

                    raise item


                yield item



    # =============================================================
    # OpenAI API
    # =============================================================


    @modal.asgi_app()
    def api(self):


        from fastapi import FastAPI

        from fastapi.responses import (
            StreamingResponse
        )

        from pydantic import BaseModel

        from typing import (
            List,
            Optional,
        )

        import json



        web = FastAPI()



        class Message(BaseModel):

            role:str

            content:str



        class RequestModel(BaseModel):

            model:str = MODEL_ID

            messages:List[Message]

            temperature:Optional[float]=0.7

            top_p:Optional[float]=0.9

            max_tokens:Optional[int]=4096

            stream:Optional[bool]=False




        @web.get(
            "/health"
        )
        async def health():

            return {
                "status":"ok"
            }




        @web.get(
            "/v1/models"
        )
        async def models():

            return {

                "object":"list",

                "data":[
                    {
                        "id":MODEL_ID,
                        "object":"model",
                        "owned_by":"self"
                    }
                ]

            }




        @web.post(
            "/v1/chat/completions"
        )
        async def chat(req:RequestModel):


            messages = [

                x.model_dump()

                for x in req.messages

            ]


            cid = (
                "chatcmpl-"
                +
                uuid.uuid4().hex
            )



            if req.stream:


                async def stream():


                    async for text in self.generate(

                        messages,

                        req.temperature,

                        req.top_p,

                        req.max_tokens,

                    ):


                        data = {

                            "id":cid,

                            "object":
                            "chat.completion.chunk",

                            "choices":[

                                {

                                "index":0,

                                "delta":
                                {
                                    "content":text
                                },

                                "finish_reason":
                                None

                                }

                            ]

                        }


                        yield (
                            "data: "
                            +
                            json.dumps(
                                data,
                                ensure_ascii=False
                            )
                            +
                            "\n\n"
                        )



                    yield "data: [DONE]\n\n"



                return StreamingResponse(
                    stream(),
                    media_type=
                    "text/event-stream",
                )



            else:


                text=""


                async for x in self.generate(

                    messages,

                    req.temperature,

                    req.top_p,

                    req.max_tokens,

                ):

                    text += x



                return {

                    "id":cid,

                    "object":
                    "chat.completion",

                    "choices":[

                        {

                        "message":
                        {
                            "role":
                            "assistant",

                            "content":
                            text
                        },

                        "finish_reason":
                        "stop"

                        }

                    ]

                }



        return web
