
import os
import subprocess
import time
import modal


# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "vaultwarden"                              # Modal App 名称
VOLUME_NAME = "vaultwarden-data"                      # 持久化数据卷名称

# 镜像配置
IMAGE = "vaultwarden/server:1.37.5"                   # 固定版本，避免 latest 自动更新

WEB_PORT = 80                                         # Vaultwarden 默认内部端口

MAX_CONTAINERS = 1                                    # 最多运行 1 个容器
MAX_CONCURRENT_INPUTS = 20                            # 单个容器最多同时处理 20 个输入

TIMEOUT_SECONDS = 3600                                # 单次函数调用最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 66                        # 空闲 180 秒后允许缩容
WEB_STARTUP_TIMEOUT_SECONDS = 120                      # Web 服务启动等待时间（秒）

# Modal Secret 名称
# 必须提前在 Modal 中创建 vaultwarden-secret
SECRET_NAME = "vaultwarden-secret"


# ---------- 环境变量说明（全部使用 VW_ 前缀） ----------

# VW_DOMAIN              ：Vaultwarden 对外访问的完整 HTTPS 地址
# VW_ADMIN_TOKEN         ：管理后台 /admin 的 Argon2id 哈希（可选）
# VW_SIGNUPS_ALLOWED     ：是否允许公开注册，true / false
# VW_INVITATIONS_ALLOWED ：是否允许邀请注册，true / false

# 首次注册自己的账号时，可以暂时设置 VW_SIGNUPS_ALLOWED=true
# 注册完成后改为 false，关闭公开注册

# 不需要管理后台时，不设置 VW_ADMIN_TOKEN 即可


# ====================================================
# 1. 创建或获取持久化数据卷
# ====================================================

# Vaultwarden 数据保存在 /data 目录
# 包括数据库、附件、密钥等
# 不要删除这个 Volume，否则可能丢失已有账号数据

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 构建 Vaultwarden 镜像
# ====================================================

# 使用 Vaultwarden 官方镜像
# 添加 Python 3.11，供 Modal 启动代码使用

image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",
)


# 创建 Modal App
app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 环境变量检查函数
# ====================================================

def get_bool_env(name: str, default: str) -> str:
    """
    读取布尔环境变量。
    只接受 true 或 false，避免传入无效配置。
    """

    value = os.environ.get(name, default).strip().lower()

    if value not in ("true", "false"):
        raise ValueError(
            f"{name} 必须填写 true 或 false，当前值：{value!r}"
        )

    return value


# ====================================================
# 4. 启动 Vaultwarden
# ====================================================

@app.function(
    max_containers=MAX_CONTAINERS,                   # 最多一个容器，避免额外启动实例
    cpu=0.125,                                         # CPU 配额
    memory=256,                                       # 内存配额，单位 MB
    volumes={"/data": vol},                           # 挂载持久化数据卷
    timeout=TIMEOUT_SECONDS,                         # 函数调用最长运行时间
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,        # 空闲缩容等待时间
    secrets=[
        modal.Secret.from_name(SECRET_NAME),          # 加载 Modal Secret
    ],
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,                 # 允许一个容器并发处理多个输入
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    """启动 Vaultwarden Web 服务。"""

    print("======================================", flush=True)
    print("正在启动 Vaultwarden...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 4.1 读取 Modal Secret
    # ------------------------------------------------

    domain = os.environ.get(
        "VW_DOMAIN",
        "",
    ).strip()

    admin_token = os.environ.get(
        "VW_ADMIN_TOKEN",
        "",
    ).strip()

    # 是否允许公开注册
    signups_allowed = get_bool_env(
        "VW_SIGNUPS_ALLOWED",
        "false",
    )

    # 是否允许邀请注册
    invitations_allowed = get_bool_env(
        "VW_INVITATIONS_ALLOWED",
        "false",
    )

    # 检查访问域名
    if not domain.startswith("https://"):
        raise ValueError(
            "VW_DOMAIN 配置错误！请填写完整的 HTTPS 地址。"
        )

    # ------------------------------------------------
    # 4.2 组装 Vaultwarden 环境变量
    # ------------------------------------------------

    env = os.environ.copy()

    # 先删除可能存在的空管理员 Token
    env.pop("ADMIN_TOKEN", None)

    # Vaultwarden 官方环境变量
    env.update({
        "DOMAIN": domain,                              # 对外访问地址
        "SIGNUPS_ALLOWED": signups_allowed,            # 是否允许公开注册
        "INVITATIONS_ALLOWED": invitations_allowed,    # 是否允许邀请注册
        "SHOW_PASSWORD_HINT": "false",                 # 不公开显示密码提示
        "ROCKET_ADDRESS": "0.0.0.0",                   # 监听所有网络接口
        "ROCKET_PORT": str(WEB_PORT),                  # 内部监听端口
        "LOG_LEVEL": "info",                           # 输出详细日志，便于排查
        "DATA_FOLDER": "/data",                        # 数据存储目录
    })

    # 只有设置了管理员 Token 才启用管理后台
    if admin_token:
        env["ADMIN_TOKEN"] = admin_token
        print("已配置管理员 Token。", flush=True)
    else:
        print("未设置管理员 Token，/admin 管理后台不可用。", flush=True)

    # ------------------------------------------------
    # 4.3 输出启动信息
    # ------------------------------------------------

    print(f"访问域名：{domain}", flush=True)
    print(f"公开注册：{signups_allowed}", flush=True)
    print(f"邀请注册：{invitations_allowed}", flush=True)
    print(f"数据目录：/data", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"最大并发输入：{MAX_CONCURRENT_INPUTS}", flush=True)

    # ------------------------------------------------
    # 4.4 启动 Vaultwarden 官方入口脚本
    # ------------------------------------------------

    # 使用官方 /start.sh 启动 Vaultwarden
    # stdout 和 stderr 继承当前容器输出，方便查看 Modal 日志

    process = subprocess.Popen(
        ["/start.sh"],
        env=env,
    )

    print(
        f"Vaultwarden 进程已启动，PID：{process.pid}",
        flush=True,
    )

    # ------------------------------------------------
    # 4.5 检查进程是否立即退出
    # ------------------------------------------------

    # 等待两秒，检查 Vaultwarden 是否发生启动错误
    # Web 服务真正就绪由 Modal 的 web_server 启动检查负责

    time.sleep(2)

    return_code = process.poll()

    if return_code is not None:
        raise RuntimeError(
            f"Vaultwarden 启动失败，进程退出代码：{return_code}"
        )

    print(
        "Vaultwarden 进程仍在运行，等待 Web 服务就绪。",
        flush=True,
    )

    # 不要在这里循环读取 stdout
    # 不要使用 while True 阻塞启动函数
    # Modal 会检查 WEB_PORT 对应的服务是否已经就绪
