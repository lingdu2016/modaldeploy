
import os
import subprocess
import time
import modal


# ============================================================
# 1. 基础配置
# ============================================================

APP_NAME = "vaultwarden"                 # Modal App 名称
VOLUME_NAME = "vaultwarden-data"         # 持久化数据卷名称

# 固定 Vaultwarden 版本，避免 latest 自动更新造成意外
IMAGE = "vaultwarden/server:1.37.5"

WEB_PORT = 80                            # Vaultwarden 容器内部监听端口

MAX_CONTAINERS = 1                       # 最多运行 1 个容器

TIMEOUT_SECONDS = 3600                   # 单次函数最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 180           # 空闲 180 秒后允许缩容
WEB_STARTUP_TIMEOUT_SECONDS = 120        # 等待 Web 服务启动的最长时间

SECRET_NAME = "vaultwarden-secret"       # Modal 中预先创建的 Secret 名称


# ============================================================
# 2. Secret 环境变量说明
# ============================================================
#
# 在 Modal 的 Secrets 页面创建 vaultwarden-secret。
#
# 需要设置：
#
# VW_DOMAIN              = https://你的实际Modal域名
# VW_SIGNUPS_ALLOWED     = true 或 false
# VW_INVITATIONS_ALLOWED = true 或 false
#
# 可选设置：
#
# VW_ADMIN_TOKEN         = Argon2id 格式的管理员密码哈希
#
# 注意：
# 1. 第一次注册自己的账号时，VW_SIGNUPS_ALLOWED=true。
# 2. 注册成功后改为 false，关闭公开注册。
# 3. 不需要 /admin 管理后台时，不设置 VW_ADMIN_TOKEN。
# 4. 不要把普通用户的登录密码写进代码或 Secret。
# 5. Secret 必须提前创建，否则部署会报错。
#
# ============================================================


# ============================================================
# 3. 创建持久化数据卷
# ============================================================

# Vaultwarden 的数据库、附件、密钥等数据保存在 /data。
# Modal Volume 用于持久化保存这些数据。
#
# 不要随意删除这个 Volume，否则可能丢失账号数据。

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
# 4. 构建容器镜像
# ============================================================

# 使用 Vaultwarden 官方 Docker 镜像。
# 添加 Python 3.11，以便运行 Modal Python 启动代码。
# Vaultwarden 本身仍然由官方镜像中的 /start.sh 启动。

image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",
)


# 创建 Modal App
app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
# 5. 环境变量布尔值检查函数
# ============================================================

def get_bool_env(name: str, default: str) -> str:
    """
    读取布尔环境变量。

    只接受 true 或 false。
    如果填写其他内容，直接报错，避免 Vaultwarden 忽略无效配置。
    """

    value = os.environ.get(name, default).strip().lower()

    if value not in ("true", "false"):
        raise ValueError(
            f"{name} 必须填写 true 或 false，当前值：{value!r}"
        )

    return value


# ============================================================
# 6. 启动 Vaultwarden Web 服务
# ============================================================

@app.function(
    # 最多运行的容器数量
    max_containers=MAX_CONTAINERS,

    # CPU 配额
    cpu=0.25,

    # 内存配额，单位 MB
    memory=512,

    # 将 Modal 持久化卷挂载到容器的 /data
    volumes={
        "/data": vol,
    },

    # 函数最长运行时间
    timeout=TIMEOUT_SECONDS,

    # 空闲后自动缩容的等待时间
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

    # 加载预先创建的 Secret
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    """
    启动 Vaultwarden。

    Modal 会等待 WEB_PORT 对应的 HTTP 服务就绪。
    """

    print("======================================", flush=True)
    print("正在启动 Vaultwarden...", flush=True)
    print("======================================", flush=True)

    # --------------------------------------------------------
    # 6.1 读取 Secret 中的配置
    # --------------------------------------------------------

    domain = os.environ.get(
        "VW_DOMAIN",
        "",
    ).strip()

    admin_token = os.environ.get(
        "VW_ADMIN_TOKEN",
        "",
    ).strip()

    # 是否允许公开注册
    # 默认 false，避免意外开放注册
    signups_allowed = get_bool_env(
        "VW_SIGNUPS_ALLOWED",
        "false",
    )

    # 是否允许邀请注册
    # 默认 false
    invitations_allowed = get_bool_env(
        "VW_INVITATIONS_ALLOWED",
        "false",
    )

    # 检查域名配置
    if not domain.startswith("https://"):
        raise ValueError(
            "VW_DOMAIN 未正确设置！"
            "请填写完整的 HTTPS 地址，例如 https://example.modal.run"
        )

    # --------------------------------------------------------
    # 6.2 组装 Vaultwarden 环境变量
    # --------------------------------------------------------

    # 复制当前容器环境变量
    env = os.environ.copy()

    # 清除可能继承的旧管理员 Token
    # 避免空字符串被传给 Vaultwarden
    env.pop("ADMIN_TOKEN", None)

    # Vaultwarden 官方程序识别的环境变量
    env.update({

        # 对外访问的 HTTPS 地址
        "DOMAIN": domain,

        # 是否允许公开注册
        "SIGNUPS_ALLOWED": signups_allowed,

        # 是否允许邀请注册
        "INVITATIONS_ALLOWED": invitations_allowed,

        # 不显示密码提示
        "SHOW_PASSWORD_HINT": "false",

        # 监听所有网络接口
        "ROCKET_ADDRESS": "0.0.0.0",

        # Vaultwarden 内部监听端口
        "ROCKET_PORT": str(WEB_PORT),

        # 使用 info 级别日志，方便排查启动问题
        "LOG_LEVEL": "info",

        # 数据存储目录
        "DATA_FOLDER": "/data",
    })

    # 只有设置了管理员 Token，才启用管理后台
    if admin_token:
        env["ADMIN_TOKEN"] = admin_token
        print("已设置管理员 Token，管理后台已配置。", flush=True)
    else:
        print(
            "未设置管理员 Token，/admin 管理后台不可用。",
            flush=True,
        )

    # --------------------------------------------------------
    # 6.3 输出启动配置
    # --------------------------------------------------------

    print(f"访问域名：{domain}", flush=True)
    print(f"公开注册：{signups_allowed}", flush=True)
    print(f"邀请注册：{invitations_allowed}", flush=True)
    print("数据目录：/data", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)

    # --------------------------------------------------------
    # 6.4 启动 Vaultwarden 官方入口脚本
    # --------------------------------------------------------

    # 不要使用 shell=True。
    # 不要在这里循环读取 stdout。
    #
    # stdout 和 stderr 继承当前容器的输出流，
    # Modal 可以收集子进程输出的日志。

    process = subprocess.Popen(
        ["/start.sh"],
        env=env,
    )

    print(
        f"Vaultwarden 子进程已启动，PID：{process.pid}",
        flush=True,
    )

    # --------------------------------------------------------
    # 6.5 检查进程是否立即退出
    # --------------------------------------------------------

    # 给 Vaultwarden 一点启动时间。
    # 如果进程立即退出，主动抛出错误，方便查看部署日志。

    time.sleep(2)

    return_code = process.poll()

    if return_code is not None:
        raise RuntimeError(
            f"Vaultwarden 启动失败，进程退出代码：{return_code}"
        )

    print(
        "Vaultwarden 进程仍在运行，等待 Modal 检查 Web 服务端口。",
        flush=True,
    )

    # 函数返回后，Modal 的 web_server 会继续检查 HTTP 服务是否就绪。
    # 不要在这里使用 while True 或循环读取 stdout。
