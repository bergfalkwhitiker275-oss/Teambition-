"""LLM 意图解析模块 - 调用大模型提取任务信息"""

import json
import logging
from datetime import date
from typing import Optional

from openai import AsyncOpenAI

from config import get_settings
from llm.prompts import SYSTEM_PROMPT, PARSE_USER_MESSAGE, QUERY_STATUS_EXAMPLES

logger = logging.getLogger(__name__)


class TaskParseResult:
    """LLM 解析结果"""

    def __init__(self, data: dict):
        self.action: str = data.get("action", "create")
        self.title: Optional[str] = data.get("title")
        self.assignee: Optional[str] = data.get("assignee")
        self.due_date: Optional[str] = data.get("due_date")
        self.start_date: Optional[str] = data.get("start_date")
        self.priority: Optional[str] = data.get("priority")
        self.project: Optional[str] = data.get("project")
        self.note: Optional[str] = data.get("note")
        self.requirement_source: Optional[str] = data.get("requirement_source")
        self.acceptor: Optional[str] = data.get("acceptor")
        self.participants: Optional[list[str]] = data.get("participants")
        self.task_type: Optional[str] = data.get("task_type")
        self.sprint: Optional[str] = data.get("sprint")
        self.target_task: Optional[str] = data.get("target_task")
        self.update_fields: Optional[dict] = data.get("update_fields")
        self.query_target: Optional[str] = data.get("query_target")
        self.query_status: Optional[str] = data.get("query_status")
        self.query_sprint: Optional[str] = data.get("query_sprint")
        self.notify: bool = bool(data.get("notify", False))
        self.status_name: Optional[str] = data.get("status_name")
        self.batch_target_user: Optional[str] = data.get("batch_target_user")
        self.batch_new_type: Optional[str] = data.get("batch_new_type")
        self.tasks: Optional[list[dict]] = data.get("tasks")
        self.raw = data

    @property
    def is_valid_for_create(self) -> bool:
        """检查是否有足够信息创建任务 (至少需要标题)"""
        return bool(self.title)

    @property
    def is_valid_for_batch_create(self) -> bool:
        """检查是否有足够信息批量创建任务"""
        return bool(self.tasks and len(self.tasks) > 0
                    and all(t.get("title") for t in self.tasks))

    @property
    def missing_fields(self) -> list[str]:
        """返回缺失的重要字段列表"""
        missing = []
        if not self.title:
            missing.append("任务标题")
        if not self.assignee:
            missing.append("负责人")
        return missing

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "title": self.title,
            "assignee": self.assignee,
            "due_date": self.due_date,
            "priority": self.priority,
            "project": self.project,
        }


async def parse_task_from_message(message: str) -> TaskParseResult:
    """
    使用 LLM 从自然语言消息中提取任务信息

    Args:
        message: 用户发送的消息文本

    Returns:
        TaskParseResult: 解析结果
    """
    settings = get_settings()

    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )

    current_date = date.today().isoformat()
    system_prompt = SYSTEM_PROMPT.format(current_date=current_date) + QUERY_STATUS_EXAMPLES.format(current_date=current_date)
    user_prompt = PARSE_USER_MESSAGE.format(message=message)

    try:
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"

        # 尝试解析 JSON
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # 尝试从回复中提取 JSON 部分
            import re
            json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                logger.error("LLM 返回内容无法解析为 JSON: %s", content)
                data = {}

        logger.info("LLM 解析结果: %s", json.dumps(data, ensure_ascii=False))
        return TaskParseResult(data)

    except Exception as e:
        logger.error("LLM 调用失败: %s", str(e))
        return TaskParseResult({})


def format_missing_info_message(result: TaskParseResult) -> str:
    """生成缺少信息的提示消息"""
    extracted = []
    if result.title:
        extracted.append(f"- 任务: {result.title}")
    if result.assignee:
        extracted.append(f"- 负责人: {result.assignee}")
    if result.due_date:
        extracted.append(f"- 截止日期: {result.due_date}")
    if result.priority:
        extracted.append(f"- 优先级: {result.priority}")

    extracted_text = "\n".join(extracted) if extracted else "- (未提取到任何信息)"
    missing_text = "\n".join(f"- {f}" for f in result.missing_fields)

    return (
        f"我从你的消息中提取到了以下信息:\n"
        f"{extracted_text}\n\n"
        f"但还缺少以下必要信息:\n"
        f"{missing_text}\n\n"
        f"请补充以上信息，我会帮你创建任务。"
    )
