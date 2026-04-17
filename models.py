"""Pydantic 数据模型定义"""

from typing import Optional
from pydantic import BaseModel, Field


class TaskInfo(BaseModel):
    """LLM 解析出的任务信息"""

    action: str = Field(default="create", description="操作类型: create/query/update")
    title: Optional[str] = Field(default=None, description="任务标题 (创建时为新任务标题)")
    assignee: Optional[str] = Field(default=None, description="负责人姓名")
    due_date: Optional[str] = Field(default=None, description="截止日期 (ISO 8601)")
    priority: Optional[str] = Field(default=None, description="优先级: high/medium/low")
    project: Optional[str] = Field(default=None, description="项目名称")
    # update 操作专用字段
    target_task: Optional[str] = Field(default=None, description="要修改的目标任务名称 (update 时必填)")
    update_fields: Optional[dict] = Field(default=None, description="要更新的字段和新值")


class DingTalkMessage(BaseModel):
    """钉钉消息结构"""

    msg_id: str = ""
    msg_type: str = "text"
    text: str = ""
    conversation_type: str = "1"  # 1=单聊, 2=群聊
    conversation_id: str = ""
    sender_id: str = ""
    sender_nick: str = ""
    sender_corp_id: str = ""
    session_webhook: str = ""
    session_webhook_expired_time: int = 0
    is_admin: bool = False
    at_users: list[dict] = Field(default_factory=list)


class TeambitionTaskEvent(BaseModel):
    """Teambition 任务事件"""

    event: str = ""  # 事件类型, 如 "task.update"
    task_id: str = Field(default="", alias="_id")
    title: str = Field(default="", alias="content")
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    executor_id: Optional[str] = None
    creator_id: Optional[str] = None
    operator_id: Optional[str] = None
    operator_name: Optional[str] = None


class BotResponse(BaseModel):
    """机器人回复结构"""

    success: bool = True
    message: str = ""
    data: Optional[dict] = None
