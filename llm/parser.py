"""LLM 意图解析模块 - 调用大模型提取任务信息"""

import asyncio
import json
import logging
import re
from datetime import date
from typing import Optional, Union

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from config import get_settings
from llm.prompts import SYSTEM_PROMPT, PARSE_USER_MESSAGE, QUERY_STATUS_EXAMPLES

import pathlib

logger = logging.getLogger(__name__)

# 备用模型状态持久化文件（进程重启后保持状态）
_STATE_FILE = pathlib.Path(__file__).parent.parent / ".llm_fallback_state"

def _load_fallback_state() -> bool:
    try:
        return _STATE_FILE.read_text().strip() == "1"
    except Exception:
        return False

def _save_fallback_state(value: bool) -> None:
    try:
        _STATE_FILE.write_text("1" if value else "0")
    except Exception as e:
        logger.warning("无法保存 fallback 状态: %s", e)

# 备用模型状态标志 — 主模型触发配额错误后切换，直到手动重置
_using_fallback: bool = _load_fallback_state()


def is_using_fallback() -> bool:
    """返回当前是否正在使用备用模型"""
    return _using_fallback


def reset_to_primary() -> None:
    """手动重置为主模型（由 '恢复主模型' 命令调用）"""
    global _using_fallback
    _using_fallback = False
    _save_fallback_state(False)
    logger.info("已手动重置为主模型")


def switch_to_fallback() -> None:
    """手动切换到备用模型（由 '切换备用模型' 命令调用）"""
    global _using_fallback
    _using_fallback = True
    _save_fallback_state(True)
    logger.info("已手动切换到备用模型")


async def _notify_admins_fallback_switch() -> None:
    """切换到备用模型后，通过钉钉私信通知项目管理员（fire-and-forget）"""
    try:
        import httpx
        import json as _json
        from teambition.client import get_teambition_client
        settings = get_settings()
        if not settings.llm_fallback_model:
            return
        tb = get_teambition_client()
        admin_ids = await tb.get_project_admins(operator_id="")
        if not admin_ids:
            logger.warning("备用模型切换通知：未找到项目管理员，跳过通知")
            return
        token = await tb._ensure_token()
        notify_md = (
            "### ⚠️ LLM 主模型配额耗尽\n\n"
            f"机器人已自动切换到备用模型：**{settings.llm_fallback_model}**\n\n"
            "如需恢复主模型，请发送：**恢复主模型**"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json={
                    "robotCode": settings.dingtalk_app_key,
                    "userIds": admin_ids,
                    "msgKey": "sampleMarkdown",
                    "msgParam": _json.dumps({
                        "title": "LLM 主模型配额耗尽，已切换备用模型",
                        "text": notify_md,
                    }),
                },
            )
            resp.raise_for_status()
        logger.info("已向 %d 个管理员发送备用模型切换通知", len(admin_ids))
    except Exception as e:
        logger.error("发送备用模型切换通知失败: %s", e)


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


async def parse_task_from_message(message: str) -> "TaskParseResult":
    """
    使用 LLM 从自然语言消息中提取任务信息。

    主模型配额耗尽（RateLimitError / AuthenticationError）时自动切换到备用模型，
    并持久保持该状态直到手动调用 reset_to_primary()。
    备用模型支持 OpenAI 兼容接口和 Anthropic 原生接口（通过 LLM_FALLBACK_PROVIDER 区分）。
    """
    global _using_fallback
    settings = get_settings()

    def _active_model(use_fallback: bool) -> str:
        return settings.llm_fallback_model if use_fallback else settings.llm_model

    def _parse_json(content: str) -> dict:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            logger.error("LLM 返回内容无法解析为 JSON: %s", content)
            return {}

    current_date = date.today().isoformat()
    system_prompt = (
        SYSTEM_PROMPT.format(current_date=current_date)
        + QUERY_STATUS_EXAMPLES.format(current_date=current_date)
    )
    user_prompt = PARSE_USER_MESSAGE.format(message=message)

    async def _call_openai(use_fallback: bool) -> "TaskParseResult":
        if use_fallback:
            client = AsyncOpenAI(
                api_key=settings.llm_fallback_api_key,
                base_url=settings.llm_fallback_base_url or None,
            )
        else:
            client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
            )
        model = _active_model(use_fallback)
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        data = _parse_json(content)
        logger.info("LLM 解析结果 (openai model=%s): %s", model, json.dumps(data, ensure_ascii=False))
        return TaskParseResult(data)

    async def _call_anthropic() -> "TaskParseResult":
        client = AsyncAnthropic(
            api_key=settings.llm_fallback_api_key,
            base_url=settings.llm_fallback_base_url or None,
        )
        model = settings.llm_fallback_model
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.1,
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        content = text_blocks[0].text if text_blocks else "{}"
        data = _parse_json(content)
        logger.info("LLM 解析结果 (anthropic model=%s): %s", model, json.dumps(data, ensure_ascii=False))
        return TaskParseResult(data)

    async def _call_fallback() -> "TaskParseResult":
        if settings.llm_fallback_provider.lower() == "anthropic":
            return await _call_anthropic()
        return await _call_openai(True)

    try:
        return await _call_openai(_using_fallback) if not _using_fallback else await _call_fallback()

    except (openai.RateLimitError, openai.AuthenticationError) as quota_err:
        if not _using_fallback:
            logger.warning("主模型配额耗尽 (%s)，切换到备用模型", type(quota_err).__name__)
            if not settings.llm_fallback_model:
                logger.warning("未配置备用模型 (LLM_FALLBACK_MODEL)，无法切换")
                return TaskParseResult({})
            _using_fallback = True
            _save_fallback_state(True)
            asyncio.create_task(_notify_admins_fallback_switch())
            try:
                return await _call_fallback()
            except Exception as fallback_err:
                logger.error("备用模型调用也失败: %s", fallback_err)
                return TaskParseResult({})
        else:
            logger.error("备用模型配额也耗尽: %s", quota_err)
            return TaskParseResult({})

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
