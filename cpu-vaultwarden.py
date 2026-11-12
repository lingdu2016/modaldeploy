import os
import subprocess
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "vaultwarden"                              # Modal App 名称
VOLUME_NAME = "vaultwarden-data"                      # 持久化数据卷名称（数据库、附件、密钥都存在这里）

# 镜像（直接在这里改）
IMAGE = "vaultwarden/server:latest"                   # 用最新版。想固定版本就改成 "vaultwarden/server:1.37.5"

WEB_PORT = 80                                         # Vaultwarden 默认端口，一般不用改
MAX_CONTAINERS = 1                                    # 自用只开 1 个实例
TIMEOUT_SECONDS = 3600                                # 单个容器最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 180                        # 无请求后多久自动缩到 0（秒），调大可减少冷启动
WEB_STARTUP_TIMEOUT_SECONDS = 60                      # 启动超时时间

# 可选 Secret 名称（不存在也不会报错）
# 推荐把敏感配置放到 Secret 里管理，而不是写死在代码中
# 创建示例：
# modal secret create vaultwarden-secret \
#   VW_DOMAIN=https://你的真实地址.modal.run \
#   VW_ADMIN_TOKEN=你的token \
#   VW_SIGNUPS_ALLOWED=true
SECRET_NAME = "vaultwarden-secret"

# ---------- 环境变量说明（全部使用 VW_ 前缀） ----------
# VW_DOMAIN              ：必须设置！最终访问的完整 HTTPS 地址（部署后才能知道真实地址）
# VW_ADMIN_TOKEN         ：管理后台 /admin 的密码（可选，不设置更安全）
# VW_SIGNUPS_ALLOWED     ：是否允许公开注册（true / false）。创建完自己的账号后建议改成 false
# VW_INVITATIONS_ALLOWED ：是否允许通过邀请注册（一般保持 false）

# ====================================================

# 创建或获取持久化卷
vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# 构建镜像
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",        # 保证有 Python 环境（虽然主要跑的是 Rust 二进制）
)

app = modal.App(name=APP_NAME, image=image)


@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=0.25,                             # 最低可到 0.125，给 0.25 更稳一点
    memory=512,                           # 实际空闲大约 50~150MB，给 512 留余量
    volumes={"/data": vol},               # 把数据卷挂载到容器的 /data 目录
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[modal.Secret.from_name(SECRET_NAME)],  # 必须先在modal添加环境变量
)
@modal.web_server(WEB_PORT, startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS)
def ui():
    """启动 Vaultwarden 服务"""
    print("🚀 正在启动 Vaultwarden...")

    # 读取环境变量（优先读 Secret 里的值，没有就用代码默认值）
    domain = os.environ.get("VW_DOMAIN", "https://请部署后替换成真实地址.modal.run")
    admin_token = os.environ.get("VW_ADMIN_TOKEN", "")
    signups_allowed = os.environ.get("VW_SIGNUPS_ALLOWED", "true")
    invitations_allowed = os.environ.get("VW_INVITATIONS_ALLOWED", "false")

    # 组装真正传给 Vaultwarden 的环境变量
    env = os.environ.copy()
    env.update({
        "DOMAIN": domain,                             # 必须是最终访问的 HTTPS 地址
        "ADMIN_TOKEN": admin_token,                   # 管理后台 /admin 的密码
        "SIGNUPS_ALLOWED": signups_allowed,           # 是否允许公开注册
        "INVITATIONS_ALLOWED": invitations_allowed,   # 是否允许邀请注册
        "SHOW_PASSWORD_HINT": "false",
        "ROCKET_ADDRESS": "0.0.0.0",                  # 必须监听 0.0.0.0，否则外部访问不到
        "ROCKET_PORT": str(WEB_PORT),
        "LOG_LEVEL": "warn",                          # 减少日志噪音
    })

    print(f"📌 当前 DOMAIN = {domain}")
    if not admin_token:
        print("⚠️  未设置 VW_ADMIN_TOKEN，管理后台 /admin 将无法使用（更安全）")
    else:
        print("✅ 已设置 ADMIN_TOKEN，可访问 /admin")

    # 启动官方入口脚本
    process = subprocess.Popen(
        ["/start.sh"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # 实时输出容器日志，方便排查问题
    for line in process.stdout:
        print(line, end="")
