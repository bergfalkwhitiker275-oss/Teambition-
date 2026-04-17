"""Teambition 事件回调处理 - 任务状态变更通知"""

import logging
from typing import Optional

from config import get_settings
from dingtalk.sender import send_task_status_notification, send_markdown_message

logger = logging.getLogger(__name__)

# Teambition 任务状态映射 (可根据实际项目自定义)
STATUS_MAP = {
    "open": "待处理",
    "active": "进行中",
    "done": "已完成",
    "suspended": "已暂停",
}


async def handle_teambition_event(event_data: dict) -> None:
    """
    处理 Teambition Webhook 事件

    阿里云版 Teambition Webhook 事件格式:
    {
        "event": "task.update",
        "data": {
            "_id": "task_id",
            "content": "任务标题",
            "_executorId": "user_id",
            "_creatorId": "user_id",
            ...
        },
        "changeData": {
            "fieldName": "isDone",
            "oldValue": false,
            "newValue": true
        },
        "operator": {
            "_id": "user_id",
            "name": "操作人姓名"
        }
    }
    """
    event_type = event_data.get("event", "")
    logger.info("收到 Teambition 事件: %s", event_type)

    # 只处理任务更新事件
    if event_type not in ("task.update", "task:update"):
        logger.info("忽略非任务更新事件: %s", event_type)
        return

    data = event_data.get("data", {})
    change_data = event_data.get("changeData", {})
    operator = event_data.get("operator", {})

    task_title = data.get("content", "未知任务")
    operator_name = operator.get("name", "未知用户")

    # 判断状态变更类型
    old_status, new_status = _extract_status_change(data, change_data)

    if not old_status and not new_status:
        logger.info("非状态变更事件, 跳过通知")
        return

    logger.info(
        "任务状态变更: '%s' %s -> %s (操作人: %s)",
        task_title, old_status, new_status, operator_name,
    )

    # 发送钉钉通知
    settings = get_settings()
    webhook_url = settings.dingtalk_robot_webhook

    if not webhook_url:
        logger.warning("未配置 DINGTALK_ROBOT_WEBHOOK, 无法发送通知")
        return

    # 构造通知消息
    lines = [
        "### 任务状态更新",
        f"**任务:** {task_title}",
        f"**状态变更:** {old_status} → {new_status}",
        f"**操作人:** {operator_name}",
    ]

    # 如果任务完成, 添加完成提示
    if new_status in ("已完成", "done"):
        lines.append("\n> 任务已完成")

    markdown_text = "\n\n".join(lines)

    await send_markdown_message(
        title="任务状态更新",
        markdown_text=markdown_text,
    )


def _extract_status_change(data: dict, change_data: dict) -> tuple[str, str]:
    """
    从事件数据中提取状态变更信息

    Returns:
        (old_status, new_status) 的元组, 如果不是状态变更则返回 ("", "")
    """
    field_name = change_data.get("fieldName", "")

    # isDone 字段变更 (完成/未完成)
    if field_name == "isDone":
        old_val = change_data.get("oldValue", False)
        new_val = change_data.get("newValue", False)
        if new_val and not old_val:
            return ("进行中", "已完成")
        elif old_val and not new_val:
            return ("已完成", "重新打开")

    # stageId 字段变更 (看板列变化, 通常代表状态)
    if field_name == "stageId":
        old_stage = change_data.get("oldValue", "")
        new_stage = change_data.get("newValue", "")
        # 阶段 ID 需要映射为可读名称, 这里返回 ID 供日志记录
        # 实际使用中可以通过 API 查询阶段名称
        return (f"阶段({old_stage[:8]}...)", f"阶段({new_stage[:8]}...)")

    # _executorId 变更 (负责人变更)
    if field_name == "_executorId":
        return ("", "")  # 负责人变更不视为状态变更

    # 通用: 检查 data 中的 isDone 字段
    if data.get("isDone"):
        return ("", "已完成")

    return ("", "")
