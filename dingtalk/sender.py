"""钉钉消息发送模块 - 支持通过 session webhook 和群机器人 webhook 发送消息"""

import logging
from typing import Optional

import httpx

from config import get_settings

logger = logging.getLogger(__name__)


async def send_text_message(
    text: str,
    session_webhook: Optional[str] = None,
    at_user_ids: Optional[list[str]] = None,
) -> bool:
    """
    发送文本消息

    Args:
        text: 消息内容
        session_webhook: 会话级 webhook (优先使用, 来自钉钉回调消息体)
        at_user_ids: 需要 @的用户 ID 列表
    """
    webhook_url = session_webhook or get_settings().dingtalk_robot_webhook
    if not webhook_url:
        logger.error("未配置钉钉 Webhook 地址")
        return False

    payload = {
        "msgtype": "text",
        "text": {"content": text},
    }

    if at_user_ids:
        payload["at"] = {"atUserIds": at_user_ids, "isAtAll": False}

    return await _send_request(webhook_url, payload)


async def send_markdown_message(
    title: str,
    markdown_text: str,
    session_webhook: Optional[str] = None,
    at_user_ids: Optional[list[str]] = None,
) -> bool:
    """
    发送 Markdown 格式消息 (用于美化任务卡片)

    Args:
        title: 消息标题
        markdown_text: Markdown 格式内容
        session_webhook: 会话级 webhook
        at_user_ids: 需要 @的用户 ID 列表
    """
    webhook_url = session_webhook or get_settings().dingtalk_robot_webhook
    if not webhook_url:
        logger.error("未配置钉钉 Webhook 地址")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown_text},
    }

    if at_user_ids:
        payload["at"] = {"atUserIds": at_user_ids, "isAtAll": False}

    return await _send_request(webhook_url, payload)


async def send_task_created_card(
    task_title: str,
    assignee: str,
    due_date: Optional[str],
    priority: Optional[str],
    project: Optional[str],
    session_webhook: Optional[str] = None,
) -> bool:
    """发送任务创建成功的卡片消息"""
    priority_map = {"high": "高", "medium": "中", "low": "低"}
    priority_text = priority_map.get(priority or "", priority or "未设置")

    lines = [
        f"### 任务创建成功",
        f"**任务:** {task_title}",
        f"**负责人:** {assignee}",
        f"**截止日期:** {due_date or '未设置'}",
        f"**优先级:** {priority_text}",
    ]
    if project:
        lines.append(f"**项目:** {project}")

    markdown_text = "\n\n".join(lines)
    return await send_markdown_message("任务创建成功", markdown_text, session_webhook)


async def send_task_status_notification(
    task_title: str,
    old_status: str,
    new_status: str,
    operator: str,
    session_webhook: Optional[str] = None,
    at_user_ids: Optional[list[str]] = None,
) -> bool:
    """发送任务状态变更通知"""
    lines = [
        f"### 任务状态更新",
        f"**任务:** {task_title}",
        f"**状态变更:** {old_status} → {new_status}",
        f"**操作人:** {operator}",
    ]
    markdown_text = "\n\n".join(lines)
    return await send_markdown_message("任务状态更新", markdown_text, session_webhook, at_user_ids)


async def _send_request(webhook_url: str, payload: dict) -> bool:
    """发送 HTTP 请求到钉钉 Webhook"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=payload)
            result = response.json()

            if result.get("errcode") == 0:
                logger.info("钉钉消息发送成功")
                return True
            else:
                logger.error("钉钉消息发送失败: %s", result)
                return False
    except Exception as e:
        logger.error("钉钉消息发送异常: %s", str(e))
        return False
