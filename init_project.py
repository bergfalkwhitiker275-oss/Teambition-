#!/usr/bin/env python3
"""
钉钉 Teambition 机器人 - 新项目一键初始化脚本

用法:
    cd dingtalk_bot
    python init_project.py

功能:
    1. 交互式收集项目配置信息
    2. 自动复制代码到新项目目录
    3. 根据输入生成 .env 配置文件
    4. 根据任务类型模式选择对应的 prompts.py 版本
"""

import os
import re
import shutil
import sys


# 当前脚本所在目录（即源项目目录）
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
# 新项目的父目录
PARENT_DIR = os.path.dirname(SOURCE_DIR)

# 复制时排除的目录和文件
EXCLUDE_DIRS = {"__pycache__", ".git", ".qoder", ".venv", "venv", ".idea"}
EXCLUDE_FILES = {".env", "init_project.py", "prompts_fixed.py"}


def prompt_input(label: str, default: str = "", required: bool = True) -> str:
    """交互式输入，支持默认值"""
    if default:
        hint = f"{label} [默认: {default}]: "
    else:
        hint = f"{label}: "

    while True:
        value = input(hint).strip()
        if not value and default:
            return default
        if not value and required:
            print(f"  ⚠ {label} 不能为空，请重新输入")
            continue
        return value


def prompt_choice(label: str, options: list[str], default: str = "") -> str:
    """交互式选择"""
    options_str = "/".join(options)
    if default:
        hint = f"{label} [{options_str}] (默认: {default}): "
    else:
        hint = f"{label} [{options_str}]: "

    while True:
        value = input(hint).strip().lower()
        if not value and default:
            return default
        if value in [o.lower() for o in options]:
            return value
        print(f"  ⚠ 请输入 {options_str} 中的一个")


def extract_id_from_url(url: str, pattern: str) -> str:
    """从 URL 中提取 ID"""
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return url  # 如果不是 URL，直接返回原值


def collect_config() -> dict:
    """交互式收集所有配置信息"""
    print()
    print("=" * 60)
    print("  钉钉 Teambition 机器人 - 新项目初始化")
    print("=" * 60)
    print()

    # ---------- 项目名称 ----------
    print("── 基本信息 ──")
    project_name = prompt_input("项目名称 (用作目录名，如 AIlab-dingbot)")

    target_dir = os.path.join(PARENT_DIR, project_name)
    if os.path.exists(target_dir):
        overwrite = input(f"  ⚠ 目录 {target_dir} 已存在，是否覆盖? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("已取消")
            sys.exit(0)

    print()

    # ---------- 钉钉配置 ----------
    print("── 钉钉开放平台配置 ──")
    print("  (在钉钉开放平台 -> 应用开发 -> 企业内部开发中获取)")
    dingtalk_app_key = prompt_input("钉钉 App Key")
    dingtalk_app_secret = prompt_input("钉钉 App Secret")
    dingtalk_webhook = prompt_input("钉钉机器人 Webhook (可选，留空跳过)", required=False)
    print()

    # ---------- Teambition 配置 ----------
    print("── Teambition 开放平台配置 ──")
    print("  (在阿里云 Teambition 开放平台 -> 创建应用后获取)")
    tb_app_id = prompt_input("Teambition App ID")
    tb_app_secret = prompt_input("Teambition App Secret")

    # 组织 ID - 默认复用
    default_org_id = "61d5472ce8368b70c0e1cf6b"
    tb_org_id = prompt_input("Teambition 组织 ID", default=default_org_id)

    # 项目 ID - 支持从 URL 提取
    print("  (可直接粘贴项目 URL，如 https://www.teambition.com/project/xxx/...)")
    project_id_input = prompt_input("Teambition 项目 ID 或项目 URL")
    tb_project_id = extract_id_from_url(project_id_input, r"/project/([a-f0-9]+)")

    # 视图 ID - 支持从 URL 提取
    print("  (可直接粘贴看板 URL，如 https://www.teambition.com/project/xxx/tasks/view/yyy/...)")
    view_id_input = prompt_input("Teambition 视图 ID 或看板 URL")
    tb_view_id = extract_id_from_url(view_id_input, r"/view/([a-f0-9]+)")

    tb_project_key = prompt_input("Teambition 项目前缀 (如 BP3、VXOB)")
    print()

    # ---------- LLM 配置 ----------
    print("── LLM 配置 ──")
    default_llm_key = "sk-f33ebfc2f5f849f2b23338ae3f5212c8"
    default_llm_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_llm_model = "qwen-plus"

    print(f"  默认复用现有 LLM 配置 ({default_llm_model})")
    reuse_llm = prompt_choice("是否复用", ["y", "n"], default="y")

    if reuse_llm == "y":
        llm_api_key = default_llm_key
        llm_base_url = default_llm_url
        llm_model = default_llm_model
    else:
        llm_api_key = prompt_input("LLM API Key")
        llm_base_url = prompt_input("LLM Base URL", default=default_llm_url)
        llm_model = prompt_input("LLM Model", default=default_llm_model)
    print()

    # ---------- 任务类型模式 ----------
    print("── 任务类型模式 ──")
    print("  multi: 支持多类型识别 (需求/任务/缺陷/美术)，根据用户消息智能判断")
    print("  fixed: 所有工单固定为「需求」类型，支持需求来源和验收人字段")
    task_type_mode = prompt_choice("任务类型模式", ["multi", "fixed"], default="multi")
    print()

    # ---------- 端口配置 ----------
    print("── 服务端口 ──")
    print("  FastAPI 端口用于接收 Teambition Webhook 回调")
    print("  如果同一台机器运行多个实例，需要使用不同端口")
    fastapi_port = prompt_input("FastAPI 端口", default="8001")
    print()

    return {
        "project_name": project_name,
        "target_dir": target_dir,
        "dingtalk_app_key": dingtalk_app_key,
        "dingtalk_app_secret": dingtalk_app_secret,
        "dingtalk_webhook": dingtalk_webhook,
        "tb_app_id": tb_app_id,
        "tb_app_secret": tb_app_secret,
        "tb_org_id": tb_org_id,
        "tb_project_id": tb_project_id,
        "tb_view_id": tb_view_id,
        "tb_project_key": tb_project_key,
        "llm_api_key": llm_api_key,
        "llm_base_url": llm_base_url,
        "llm_model": llm_model,
        "task_type_mode": task_type_mode,
        "fastapi_port": fastapi_port,
    }


def copy_project_files(target_dir: str):
    """复制项目代码文件到目标目录"""
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)

    def ignore_patterns(directory, contents):
        ignored = set()
        for item in contents:
            if item in EXCLUDE_DIRS:
                ignored.add(item)
            elif item in EXCLUDE_FILES:
                ignored.add(item)
            # 排除 llm/prompts_fixed.py
            elif item == "prompts_fixed.py" and os.path.basename(directory) == "llm":
                ignored.add(item)
        return ignored

    shutil.copytree(SOURCE_DIR, target_dir, ignore=ignore_patterns)
    print(f"  ✓ 代码文件已复制到 {target_dir}")


def generate_env_file(config: dict):
    """根据配置生成 .env 文件"""
    env_content = f"""# ============================
# 钉钉机器人配置
# ============================
DINGTALK_APP_KEY={config['dingtalk_app_key']}
DINGTALK_APP_SECRET={config['dingtalk_app_secret']}
DINGTALK_ROBOT_WEBHOOK={config['dingtalk_webhook']}

# ============================
# Teambition 配置 (阿里云版)
# ============================
TEAMBITION_APP_ID={config['tb_app_id']}
TEAMBITION_APP_SECRET={config['tb_app_secret']}
TEAMBITION_ORG_ID={config['tb_org_id']}
TEAMBITION_DEFAULT_PROJECT_ID={config['tb_project_id']}
TEAMBITION_DEFAULT_VIEW_ID={config['tb_view_id']}
TEAMBITION_PROJECT_KEY={config['tb_project_key']}

# ============================
# LLM 配置
# ============================
LLM_API_KEY={config['llm_api_key']}
LLM_BASE_URL={config['llm_base_url']}
LLM_MODEL={config['llm_model']}
"""
    env_path = os.path.join(config["target_dir"], ".env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)
    print(f"  ✓ .env 配置文件已生成")


def select_prompts(config: dict):
    """根据任务类型模式选择对应的 prompts.py"""
    target_prompts = os.path.join(config["target_dir"], "llm", "prompts.py")

    if config["task_type_mode"] == "fixed":
        # 使用固定需求版
        source_prompts = os.path.join(SOURCE_DIR, "llm", "prompts_fixed.py")
        shutil.copy2(source_prompts, target_prompts)
        print(f"  ✓ prompts.py 已设置为「固定需求」模式")
    else:
        # multi 模式使用默认复制过来的 prompts.py，无需额外操作
        print(f"  ✓ prompts.py 已设置为「多类型识别」模式")


def update_fastapi_port(config: dict):
    """更新 main_stream.py 中的 FastAPI 端口"""
    port = config["fastapi_port"]
    if port == "8001":
        print(f"  ✓ FastAPI 端口保持默认 (8001)")
        return

    main_stream_path = os.path.join(config["target_dir"], "main_stream.py")
    with open(main_stream_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 替换端口
    content = content.replace(
        'uvicorn.run(app, host="0.0.0.0", port=8001,',
        f'uvicorn.run(app, host="0.0.0.0", port={port},',
    )
    content = content.replace(
        "FastAPI 服务已启动 (端口 8001,",
        f"FastAPI 服务已启动 (端口 {port},",
    )

    with open(main_stream_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ FastAPI 端口已设置为 {port}")


def print_summary(config: dict):
    """输出汇总信息"""
    print()
    print("=" * 60)
    print("  初始化完成!")
    print("=" * 60)
    print()
    print(f"  项目目录: {config['target_dir']}")
    print(f"  钉钉 App Key: {config['dingtalk_app_key']}")
    print(f"  TB 项目 ID: {config['tb_project_id']}")
    print(f"  任务类型模式: {config['task_type_mode']}")
    print(f"  FastAPI 端口: {config['fastapi_port']}")
    print()
    print("  启动命令:")
    print(f"    cd {config['target_dir']}")
    print(f"    python main_stream.py")
    print()
    print("  启动前请确保:")
    print("    1. 钉钉开放平台已开启机器人能力 (Stream 模式) 并开通所需权限")
    print("    2. Teambition 开放平台已开通所需 API 权限并发布应用")
    print("    3. 已安装 Python 依赖: pip install -r requirements.txt")
    print()


def main():
    try:
        config = collect_config()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(0)

    # 确认
    print("── 确认信息 ──")
    print(f"  项目目录: {config['target_dir']}")
    print(f"  钉钉 App Key: {config['dingtalk_app_key']}")
    print(f"  TB App ID: {config['tb_app_id']}")
    print(f"  TB 项目 ID: {config['tb_project_id']}")
    print(f"  任务类型: {config['task_type_mode']}")
    print(f"  端口: {config['fastapi_port']}")
    print()

    try:
        confirm = input("确认创建? [Y/n]: ").strip().lower()
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(0)

    if confirm == "n":
        print("已取消")
        sys.exit(0)

    print()
    print("── 开始初始化 ──")

    # 1. 复制代码
    copy_project_files(config["target_dir"])

    # 2. 生成 .env
    generate_env_file(config)

    # 3. 选择 prompts 版本
    select_prompts(config)

    # 4. 更新端口
    update_fastapi_port(config)

    # 5. 输出汇总
    print_summary(config)


if __name__ == "__main__":
    main()
