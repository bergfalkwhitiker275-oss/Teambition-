"""
钉钉 Teambition 任务机器人 - FastAPI 主入口

启动方式:
    cd dingtalk_bot
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import get_settings
from dingtalk.webhook import parse_dingtalk_message
from dingtalk.sender import (
    send_text_message,
    send_task_created_card,
    send_task_status_notification,
)
from teambition.client import get_teambition_client
from teambition.webhook import handle_teambition_event
from llm.parser import parse_task_from_message, format_missing_info_message

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="钉钉 Teambition 任务机器人", version="1.0.0")


# ============================================================
# 健康检查
# ============================================================


@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "ok", "service": "dingtalk-teambition-bot"}


# ============================================================
# 钉钉消息回调
# ============================================================


@app.post("/dingtalk/callback")
async def dingtalk_callback(request: Request):
    """
    接收钉钉机器人消息回调

    流程:
    1. 解析并验证钉钉消息
    2. 使用 LLM 提取任务信息
    3. 调用 Teambition API 创建任务
    4. 通过钉钉回复创建结果
    """
    try:
        # 1. 解析钉钉消息
        msg = await parse_dingtalk_message(request)
        text = msg.get("text", "").strip()
        session_webhook = msg.get("session_webhook", "")
        sender_nick = msg.get("sender_nick", "未知用户")

        if not text:
            await send_text_message(
                "请发送任务相关的文字消息，例如:\n"
                '"让小明下周五之前完成首页设计"',
                session_webhook=session_webhook,
            )
            return {"success": True}

        logger.info("处理来自 %s 的消息: %s", sender_nick, text)

        # 2. LLM 解析意图
        parse_result = await parse_task_from_message(text)
        sender_id = msg.get("sender_id", "")

        # 3. 根据意图执行操作
        if parse_result.action == "create":
            return await _handle_create_task(parse_result, session_webhook, sender_nick, sender_id)
        elif parse_result.action == "query":
            await send_text_message(
                "查询任务功能开发中，敬请期待！",
                session_webhook=session_webhook,
            )
            return {"success": True}
        elif parse_result.action == "update":
            await send_text_message(
                "更新任务功能开发中，敬请期待！",
                session_webhook=session_webhook,
            )
            return {"success": True}
        else:
            await send_text_message(
                "我没太理解你的意思，你可以试试:\n"
                '- "让小明下周五之前完成首页设计"\n'
                '- "给张三创建一个紧急任务：修复登录Bug"',
                session_webhook=session_webhook,
            )
            return {"success": True}

    except Exception as e:
        logger.exception("处理钉钉消息时发生错误")
        return JSONResponse(
            status_code=200,  # 钉钉要求必须返回 200
            content={"success": False, "message": str(e)},
        )


async def _handle_create_task(
    parse_result, session_webhook: str, sender_nick: str, sender_id: str
) -> dict:
    """处理创建任务的逻辑"""

    # 检查必要信息是否完整
    if not parse_result.is_valid_for_create:
        missing_msg = format_missing_info_message(parse_result)
        await send_text_message(missing_msg, session_webhook=session_webhook)
        return {"success": True}

    tb_client = get_teambition_client()

    # 操作者 ID: 使用钉钉消息发送者的 userId
    operator_id = sender_id
    if not operator_id:
        await send_text_message(
            "无法获取你的用户ID，请确认机器人权限配置正确。",
            session_webhook=session_webhook,
        )
        return {"success": False, "message": "missing sender_id"}

    # 解析负责人
    assignee_id = None
    assignee_name = parse_result.assignee or sender_nick
    if parse_result.assignee:
        assignee_id = await tb_client.resolve_user_id(parse_result.assignee, operator_id=operator_id)
        if not assignee_id:
            await send_text_message(
                f"未找到负责人「{parse_result.assignee}」，任务将创建但不指定负责人。",
                session_webhook=session_webhook,
            )

    # 优先级映射
    priority_map = {"high": 2, "medium": 0, "low": 0}
    priority = priority_map.get(parse_result.priority or "medium", 0)

    # 创建任务
    try:
        result = await tb_client.create_task(
            title=parse_result.title,
            operator_id=operator_id,
            assignee_id=assignee_id,
            due_date=parse_result.due_date,
            priority=priority,
            note=f"由 {sender_nick} 通过钉钉机器人创建",
        )

        # 回复成功消息
        await send_task_created_card(
            task_title=parse_result.title,
            assignee=assignee_name,
            due_date=parse_result.due_date,
            priority=parse_result.priority,
            project=parse_result.project,
            session_webhook=session_webhook,
        )

        return {"success": True, "task_id": result.get("taskId")}

    except Exception as e:
        logger.exception("创建 Teambition 任务失败")
        await send_text_message(
            f"创建任务失败: {str(e)}\n请检查配置或稍后重试。",
            session_webhook=session_webhook,
        )
        return {"success": False, "message": str(e)}


# ============================================================
# Teambition 事件回调
# ============================================================


@app.post("/teambition/webhook")
async def teambition_webhook(request: Request):
    """
    接收 Teambition 任务状态变更事件

    流程:
    1. 解析事件数据
    2. 查找相关人员
    3. 通过钉钉发送通知
    """
    try:
        body = await request.json()
        await handle_teambition_event(body)
        return {"success": True}
    except Exception as e:
        logger.exception("处理 Teambition 事件时发生错误")
        return {"success": False, "message": str(e)}


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    logger.info("启动钉钉 Teambition 任务机器人...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
