# =============================================================================
# Qwen3.6-14B-A3B-FableVibes-GGUF
# Modal L4 + llama.cpp + OpenAI API
# Personal Novel Assistant
# =============================================================================

import os
import json

import modal


# =============================================================================
# 模型配置
# =============================================================================

MODEL_REPO = "tvall43/Qwen3.6-14B-A3B-FableVibes-GGUF"

MODEL_FILE = (
    "Qwen3.6-14B-A3B-FableVibes-Q4_K_M.gguf"
)

MODEL_NAME = "qwen3.6-14b"


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
        "uvicorn",
        "huggingface_hub>=0.23.0",
        "llama-cpp-python",
        extra_index_url=
        "https://abetlen.github.io/llama-cpp-python/whl/cu121"
    )
)


# =============================================================================
# Modal
# =============================================================================

app = modal.App(
    "qwen36-14b-personal-api"
)


volume = modal.Volume.from_name(
    "qwen36-14b-model-cache",
    create_if_missing=True
)


# =============================================================================
# 模型服务
# =============================================================================

@app.cls(
    image=image,
    gpu="L4",
    volumes={
        "/models": volume
    },
    timeout=900,
    scaledown_window=300,
    max_containers=1
)
class Model:

    @modal.enter()
    def load(self):

        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama


        model_path = (
            f"/models/{MODEL_FILE}"
        )


        if not os.path.exists(model_path):

            print(
                "Downloading model..."
            )

            hf_hub_download(
                repo_id=MODEL_REPO,
                filename=MODEL_FILE,
                local_dir="/models"
            )

            volume.commit()


        print(
            "Loading llama.cpp..."
        )


        self.llm = Llama(

            model_path=model_path,

            # L4全部GPU
            n_gpu_layers=-1,

            # 小说上下文
            n_ctx=32768,

            verbose=False

        )


        print(
            "Model ready"
        )



    def generate(
        self,
        messages,
        stream=False
    ):


        system = {

            "role":
            "system",

            "content":
            """
你是一个中文AI助手。

要求：

1. 默认使用简体中文。
2. 可以聊天、编程、知识问答。
3. 擅长长篇小说创作。


小说规则：

- 保持人物性格一致。
- 保持世界观连续。
- 不重复已经发生剧情。
- 加强环境、动作、心理描写。
- 输出正文，不解释写作过程。

不要模拟用户。
"""
        }


        final_messages = [
            system
        ]


        for m in messages:

            if m.get("role") != "system":

                final_messages.append(m)



        return self.llm.create_chat_completion(

            messages=final_messages,

            max_tokens=4096,

            temperature=0.75,

            top_p=0.9,

            repeat_penalty=1.15,

            stream=stream
        )



# =============================================================================
# OpenAI API
# =============================================================================

@app.function(
    image=image,
    timeout=900
)
@modal.web_server(
    port=8000,
    startup_timeout=900
)
def web():

    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse


    api = FastAPI()


    model = Model()



    @api.get("/health")
    async def health():

        return {
            "status":"ok",
            "model":MODEL_NAME
        }



    @api.get("/v1/models")
    async def models():

        return {

            "object":"list",

            "data":[

                {
                    "id":MODEL_NAME,
                    "object":"model"
                }

            ]

        }



    @api.post("/v1/chat/completions")
    async def chat(
        request:Request
    ):


        body = await request.json()


        messages = body.get(
            "messages",
            []
        )


        stream = body.get(
            "stream",
            False
        )


        result = model.generate(
            messages,
            stream
        )



        if stream:


            async def event():

                for chunk in result:

                    delta = (
                        chunk
                        ["choices"]
                        [0]
                        .get(
                            "delta",
                            {}
                        )
                    )


                    text = delta.get(
                        "content"
                    )


                    if text:

                        data = {

                            "choices":[

                                {
                                    "delta":{
                                        "content":text
                                    }
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

                event(),

                media_type=
                "text/event-stream"

            )



        else:


            text = (

                result
                ["choices"]
                [0]
                ["message"]
                ["content"]

            )


            return {

                "id":
                "chatcmpl-qwen",

                "object":
                "chat.completion",

                "model":
                MODEL_NAME,


                "choices":[

                    {

                        "index":0,

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



    return api
