"""
钉钉 Teambition 任务机器人 - Stream 模式入口

Stream 模式通过 WebSocket 长连接接收钉钉消息，无需公网地址。
同时启动 FastAPI 服务接收 Teambition Webhook 回调。

支持操作: 创建/修改/完成/重新打开/删除/查询任务, 设置状态

启动方式:
    cd dingtalk_bot
    python main_stream.py
"""

import asyncio
import json
import logging
import sys
import threading

import httpx
import requests
import dingtalk_stream
from dingtalk_stream import AckMessage

from config import get_settings
from teambition.client import get_teambition_client
from llm.parser import parse_task_from_message, format_missing_info_message

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# 优先级映射
PRIORITY_TO_INT = {"high": 2, "medium": 0, "low": -10}
PRIORITY_TO_TEXT = {"high": "高", "medium": "普通", "low": "较低"}
INT_TO_PRIORITY_TEXT = {2: "非常紧急", 1: "紧急", 0: "普通", -10: "较低"}

HELP_TEXT = """### Teambition 任务管理机器人

**创建任务:**
- "给吕鑫下周五之前完成首页设计"
- "创建一个紧急任务：修复登录Bug，负责人张三"

**修改任务:**
- "把首页设计的优先级改为高"
- "把首页设计的截止日期改到下周三"
- "把首页设计的负责人改为庄健男"
- "给首页设计添加备注：需要参考竞品"
- "把吕鑫加为首页设计的参与者"

**完成/重开任务:**
- "把首页设计标记为完成"
- "重新打开首页设计"

**设置工作流状态:**
- "把首页设计设为进行中"

**删除任务:**
- "删除任务测试任务"

**查询任务:**
- "查看我的任务"
- "查看吕鑫的任务"
- "查看任务首页设计的详情"
"""


class TaskBotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉机器人消息处理器 - Stream 模式"""

    def __init__(self, logger: logging.Logger = None):
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        if logger:
            self.logger = logger

    def reply_markdown_at(self, title: str, text: str, incoming_message, at_user_ids: list[str] = None):
        """发送 Markdown 回复并 @指定用户"""
        at_ids = list(set((at_user_ids or []) + [incoming_message.sender_staff_id]))
        values = {
            'msgtype': 'markdown',
            'markdown': {'title': title, 'text': text},
            'at': {'atUserIds': at_ids},
        }
        try:
            response = requests.post(
                incoming_message.session_webhook,
                headers={'Content-Type': 'application/json', 'Accept': '*/*'},
                data=json.dumps(values),
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error('reply_markdown_at failed: %s', e)
            return None

    async def _notify_assignees_via_dm(
        self, tb, tasks_by_user: dict[str, list[dict]], title: str,
        project_id: str = None,
    ):
        """
        通过机器人单聊向每个负责人发送私信通知（仅包含其个人相关任务）

        tasks_by_user: {userId: [task_dict, ...]}
        """
        settings = get_settings()
        token = await tb._ensure_token()
        pid = project_id or settings.teambition_default_project_id
        vid = settings.teambition_default_view_id

        async with httpx.AsyncClient() as client:
            for user_id, user_tasks in tasks_by_user.items():
                name = tb.resolve_user_name(user_id)
                # 构建该用户的任务摘要（含跳转链接）
                task_lines = []
                for t in user_tasks:
                    content = t.get("content", "")
                    flow = t.get("taskflowStatusName", "")
                    due = t.get("dueDate", "")
                    task_id = t.get("taskId", "")
                    due_text = f" | 截止: {due[:10]}" if due else ""
                    flow_text = f" | {flow}" if flow else ""
                    link = f"https://www.teambition.com/project/{pid}/tasks/view/{vid}/task/{task_id}" if task_id else ""
                    line = f"- [{content}]({link}){flow_text}{due_text}" if link else f"- {content}{flow_text}{due_text}"
                    task_lines.append(line)

                task_summary = "\n".join(task_lines)
                notify_text = (
                    f"### 📢 {name}，你有 {len(user_tasks)} 个任务需要关注\n\n"
                    f"**{title}**\n\n"
                    f"{task_summary}\n\n"
                    f"---\n\n请点击任务名称查看详情并及时处理。"
                )

                try:
                    resp = await client.post(
                        "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                        headers={
                            "x-acs-dingtalk-access-token": token,
                            "Content-Type": "application/json",
                        },
                        json={
                            "robotCode": settings.dingtalk_app_key,
                            "userIds": [user_id],
                            "msgKey": "sampleMarkdown",
                            "msgParam": json.dumps({
                                "title": f"任务通知：你有{len(user_tasks)}个{title}",
                                "text": notify_text,
                            }),
                        },
                    )
                    resp.raise_for_status()
                    self.logger.info("已向 %s 发送 %d 个任务通知私信", name, len(user_tasks))
                except Exception as e:
                    self.logger.error("向 %s 发送任务通知私信失败: %s", name, e)

    async def _notify_admins_new_task(
        self, tb, task_id: str, title: str, assignee_name: str,
        creator_name: str, creator_id: str,
        sprint_text: str = "", due_date: str = "",
    ):
        """任务创建后通知项目管理员"""
        try:
            admin_ids = await tb.get_project_admins(creator_id)
            # 排除创建者自己（如果管理员自己创建了任务，不需要通知自己）
            admin_ids = [uid for uid in admin_ids if uid != creator_id]
            if not admin_ids:
                self.logger.info("无需通知管理员（创建者即管理员或无管理员）")
                return

            settings = get_settings()
            link = self._task_link(task_id)
            link_text = f"\n\n🔗 [点击查看任务]({link})" if link else ""
            due_text = f"\n**截止日期:** {due_date[:10]}" if due_date else ""
            sprint_line = f"\n**迭代:** {sprint_text}" if sprint_text else ""

            notify_md = (
                f"### 📌 新任务创建通知\n\n"
                f"**任务:** {title}\n"
                f"**负责人:** {assignee_name}\n"
                f"**创建人:** {creator_name}"
                f"{due_text}{sprint_line}{link_text}"
            )

            token = await tb._ensure_token()
            async with httpx.AsyncClient() as client:
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
                        "msgParam": json.dumps({
                            "title": f"新任务: {title}",
                            "text": notify_md,
                        }),
                    },
                )
                resp.raise_for_status()
                self.logger.info("已向 %d 个管理员发送新任务通知", len(admin_ids))
        except Exception as e:
            self.logger.error("通知管理员失败: %s", e)

    def _task_link(self, task_id: str) -> str:
        """生成 Teambition 任务跳转链接"""
        settings = get_settings()
        pid = settings.teambition_default_project_id
        vid = settings.teambition_default_view_id
        if task_id and pid and vid:
            return f"https://www.teambition.com/project/{pid}/tasks/view/{vid}/task/{task_id}"
        return ""

    def _task_link_md(self, task_id: str, text: str = "点击查看") -> str:
        """生成 Markdown 格式的任务跳转链接"""
        link = self._task_link(task_id)
        return f"[{text}]({link})" if link else ""

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """处理收到的钉钉消息"""
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            text = incoming_message.text.content.strip() if incoming_message.text else ""
            # sender_staff_id 是组织内真实 userId，sender_id 是加密格式不可用
            sender_id = incoming_message.sender_staff_id or incoming_message.sender_id
            sender_nick = incoming_message.sender_nick
            conversation_type = incoming_message.conversation_type

            self.logger.info(
                "收到消息: sender=%s (staffId=%s), type=%s, text=%s",
                sender_nick, sender_id, conversation_type, text,
            )

            if not text:
                self.reply_text("请发送任务相关指令，发送 \"帮助\" 查看支持的操作。", incoming_message)
                return AckMessage.STATUS_OK, "OK"

            # 1. LLM 解析意图
            parse_result = await parse_task_from_message(text)

            # 2. 根据意图分发
            action = parse_result.action
            handler_map = {
                "create": self._handle_create,
                "update": self._handle_update,
                "complete": self._handle_complete,
                "reopen": self._handle_reopen,
                "delete": self._handle_delete,
                "query": self._handle_query,
                "status": self._handle_status,
                "help": self._handle_help,
            }

            handler = handler_map.get(action)
            if handler:
                await handler(parse_result, incoming_message, sender_id, sender_nick)
            else:
                self.reply_markdown("帮助", HELP_TEXT, incoming_message)

        except Exception as e:
            self.logger.exception("处理消息时发生错误")
            try:
                self.reply_text(f"处理消息时出错: {str(e)}", incoming_message)
            except Exception:
                pass

        return AckMessage.STATUS_OK, "OK"

    # ==============================================================
    # 创建任务
    # ==============================================================

    async def _handle_create(self, pr, msg, sender_id, sender_nick):
        if not pr.is_valid_for_create:
            missing_msg = format_missing_info_message(pr)
            self.reply_text(missing_msg, msg)
            return

        if not sender_id:
            self.reply_text("无法获取你的用户ID，请确认机器人权限配置正确。", msg)
            return

        tb = get_teambition_client()

        # 解析负责人：未指定时默认为发送者自己
        assignee_id = None
        assignee_name = pr.assignee or sender_nick
        if pr.assignee:
            assignee_id = await tb.resolve_user_id(pr.assignee, operator_id=sender_id)
            if not assignee_id:
                self.reply_text(f"未找到项目成员「{pr.assignee}」，任务将创建但不指定负责人。", msg)
        else:
            # 未指定负责人，默认为发送者
            assignee_id = sender_id
            assignee_name = sender_nick

        priority = PRIORITY_TO_INT.get(pr.priority or "medium", 0) if pr.priority else None

        try:
            result = await tb.create_task(
                title=pr.title,
                operator_id=sender_id,
                assignee_id=assignee_id,
                due_date=pr.due_date,
                priority=priority,
                note=pr.note or f"由 {sender_nick} 通过钉钉机器人创建",
            )

            # 如果有参与者，额外添加
            task_id = result.get("taskId", "")
            if pr.participants and task_id:
                add_ids = []
                for name in pr.participants:
                    uid = await tb.resolve_user_id(name, operator_id=sender_id)
                    if uid:
                        add_ids.append(uid)
                if add_ids:
                    await tb.update_task_participants(task_id, sender_id, add_ids=add_ids)

            # 如果有开始日期
            if pr.start_date and task_id:
                await tb.update_task_start_date(task_id, sender_id, pr.start_date)

            # 如果有迭代
            sprint_text = ""
            if pr.sprint and task_id:
                sprint_id = await tb.resolve_sprint_id(pr.sprint, sender_id)
                if sprint_id:
                    try:
                        await tb.update_task_sprint(task_id, sender_id, sprint_id)
                        sprint_text = pr.sprint
                    except Exception as e:
                        self.logger.error("创建任务时设置迭代失败: %s", e)
                        sprint_text = f"设置失败"
                else:
                    sprint_text = f"未找到『{pr.sprint}』"

            priority_text = PRIORITY_TO_TEXT.get(pr.priority, "普通") if pr.priority else "普通"
            md = [
                "### 任务创建成功",
                f"**任务:** {pr.title}",
                f"**负责人:** {assignee_name}",
                f"**截止日期:** {pr.due_date or '未设置'}",
                f"**优先级:** {priority_text}",
            ]
            if pr.start_date:
                md.append(f"**开始日期:** {pr.start_date}")
            if pr.note:
                md.append(f"**备注:** {pr.note}")
            if pr.participants:
                md.append(f"**参与者:** {', '.join(pr.participants)}")
            if sprint_text:
                md.append(f"**迭代:** {sprint_text}")
            link_md = self._task_link_md(task_id)
            if link_md:
                md.append(f"\n🔗 {link_md}")

            # 提交代码
            unique_id = result.get("uniqueId", 0)
            submit_code = tb.format_submit_code(task_id, unique_id, pr.title, assignee_name)
            if submit_code:
                md.append(f"\n**提交代码:** ⬇️ 见下条消息，可右键直接复制")

            self.reply_markdown("任务创建成功", "\n\n".join(md), msg)
            self.logger.info("任务创建成功: taskId=%s", task_id)

            # 单独发送提交代码文本，方便用户一键复制
            if submit_code:
                self.reply_text(submit_code, msg)

            # 通知项目管理员
            if task_id:
                await self._notify_admins_new_task(
                    tb, task_id, pr.title, assignee_name, sender_nick, sender_id,
                    sprint_text=sprint_text,
                    due_date=pr.due_date,
                )

        except Exception as e:
            self.logger.exception("创建任务失败")
            self.reply_text(f"创建任务失败: {str(e)}", msg)

    # ==============================================================
    # 修改任务
    # ==============================================================

    async def _handle_update(self, pr, msg, sender_id, sender_nick):
        if not pr.target_task:
            self.reply_text("请告诉我要修改哪个任务，例如：\n\"把任务'首页设计'的优先级改为高\"", msg)
            return
        if not pr.update_fields:
            self.reply_text(f"请告诉我要修改任务『{pr.target_task}』的什么属性。\n支持: 优先级/负责人/截止日期/开始日期/标题/备注/参与者", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
        if not task:
            self.reply_text(f"未找到任务『{pr.target_task}』，请确认任务名称是否正确。", msg)
            return

        task_id = task.get("taskId", "")
        task_title = task.get("content", pr.target_task)
        fields = pr.update_fields
        updated = []

        try:
            if "priority" in fields:
                val = PRIORITY_TO_INT.get(fields["priority"], 0)
                await tb.update_task_priority(task_id, sender_id, val)
                updated.append(f"**优先级:** {PRIORITY_TO_TEXT.get(fields['priority'], fields['priority'])}")

            if "assignee" in fields:
                uid = await tb.resolve_user_id(fields["assignee"], operator_id=sender_id)
                if uid:
                    await tb.update_task_executor(task_id, sender_id, uid)
                    updated.append(f"**负责人:** {fields['assignee']}")
                else:
                    updated.append(f"**负责人:** 未找到『{fields['assignee']}』，跳过")

            if "due_date" in fields:
                await tb.update_task_due_date(task_id, sender_id, fields["due_date"])
                updated.append(f"**截止日期:** {fields['due_date']}")

            if "start_date" in fields:
                await tb.update_task_start_date(task_id, sender_id, fields["start_date"])
                updated.append(f"**开始日期:** {fields['start_date']}")

            if "title" in fields:
                await tb.update_task_content(task_id, sender_id, fields["title"])
                updated.append(f"**新标题:** {fields['title']}")

            if "note" in fields:
                await tb.update_task_note(task_id, sender_id, fields["note"])
                updated.append(f"**备注:** {fields['note']}")

            if "add_participants" in fields:
                add_ids = []
                names = []
                for name in fields["add_participants"]:
                    uid = await tb.resolve_user_id(name, operator_id=sender_id)
                    if uid:
                        add_ids.append(uid)
                        names.append(name)
                if add_ids:
                    await tb.update_task_participants(task_id, sender_id, add_ids=add_ids)
                    updated.append(f"**添加参与者:** {', '.join(names)}")

            if "del_participants" in fields:
                del_ids = []
                names = []
                for name in fields["del_participants"]:
                    uid = await tb.resolve_user_id(name, operator_id=sender_id)
                    if uid:
                        del_ids.append(uid)
                        names.append(name)
                if del_ids:
                    await tb.update_task_participants(task_id, sender_id, del_ids=del_ids)
                    updated.append(f"**移除参与者:** {', '.join(names)}")

            if "sprint" in fields:
                sprint_name = fields["sprint"]
                sprint_id = await tb.resolve_sprint_id(sprint_name, sender_id)
                if sprint_id:
                    await tb.update_task_sprint(task_id, sender_id, sprint_id)
                    updated.append(f"**迭代:** {sprint_name}")
                else:
                    updated.append(f"**迭代:** 未找到『{sprint_name}』，跳过")

            if updated:
                link_md = self._task_link_md(task_id)
                link_line = f"\n\n🔗 {link_md}" if link_md else ""
                md = ["### 任务更新成功", f"**任务:** {task_title}", "", "已更新："] + updated + [link_line]
                self.reply_markdown("任务更新成功", "\n\n".join(md), msg)
            else:
                self.reply_text("没有识别到需要更新的内容。", msg)

            self.logger.info("任务更新成功: taskId=%s, fields=%s", task_id, list(fields.keys()))

        except Exception as e:
            self.logger.exception("更新任务失败")
            self.reply_text(f"更新任务失败: {str(e)}", msg)

    # ==============================================================
    # 完成任务
    # ==============================================================

    async def _handle_complete(self, pr, msg, sender_id, sender_nick):
        if not pr.target_task:
            self.reply_text("请告诉我要完成哪个任务，例如：\"把首页设计标记为完成\"", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
        if not task:
            self.reply_text(f"未找到任务『{pr.target_task}』。", msg)
            return

        try:
            await tb.complete_task(task["taskId"], sender_id)
            self.reply_markdown(
                "任务已完成",
                f"### 任务已完成 ✓\n\n**任务:** {task.get('content', pr.target_task)}\n\n🔗 {self._task_link_md(task['taskId'])}",
                msg,
            )
            self.logger.info("任务已完成: %s", task["taskId"])
        except Exception as e:
            self.logger.exception("完成任务失败")
            self.reply_text(f"完成任务失败: {str(e)}", msg)

    # ==============================================================
    # 重新打开任务
    # ==============================================================

    async def _handle_reopen(self, pr, msg, sender_id, sender_nick):
        if not pr.target_task:
            self.reply_text("请告诉我要重新打开哪个任务。", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
        if not task:
            self.reply_text(f"未找到任务『{pr.target_task}』。", msg)
            return

        try:
            await tb.reopen_task(task["taskId"], sender_id)
            self.reply_markdown(
                "任务已重新打开",
                f"### 任务已重新打开\n\n**任务:** {task.get('content', pr.target_task)}\n\n🔗 {self._task_link_md(task['taskId'])}",
                msg,
            )
        except Exception as e:
            self.logger.exception("重新打开任务失败")
            self.reply_text(f"重新打开任务失败: {str(e)}", msg)

    # ==============================================================
    # 删除任务
    # ==============================================================

    async def _handle_delete(self, pr, msg, sender_id, sender_nick):
        if not pr.target_task:
            self.reply_text("请告诉我要删除哪个任务。", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
        if not task:
            self.reply_text(f"未找到任务『{pr.target_task}』。", msg)
            return

        try:
            await tb.delete_task(task["taskId"], sender_id)
            self.reply_markdown(
                "任务已删除",
                f"### 任务已删除\n\n**任务:** {task.get('content', pr.target_task)}",
                msg,
            )
            self.logger.info("任务已删除: %s", task["taskId"])
        except Exception as e:
            self.logger.exception("删除任务失败")
            self.reply_text(f"删除任务失败: {str(e)}", msg)

    # ==============================================================
    # 查询任务
    # ==============================================================

    async def _handle_query(self, pr, msg, sender_id, sender_nick):
        tb = get_teambition_client()
        query_target = pr.query_target
        query_status = pr.query_status
        query_sprint = getattr(pr, 'query_sprint', None)

        try:
            # 如果指定了迭代，先解析 sprintId
            sprint_id = None
            sprint_name = None
            if query_sprint:
                sprint_id = await tb.resolve_sprint_id(query_sprint, sender_id)
                sprint_name = query_sprint
                if not sprint_id:
                    self.reply_text(f"未找到迭代『{query_sprint}』。", msg)
                    return

            # 如果指定了迭代，使用 Teambition 开放平台 API 直接查询迭代下的任务
            if sprint_id:
                tasks = await tb.query_tasks_by_sprint(sprint_id, sender_id)
                title = f"迭代「{sprint_name}」的任务"

                # 如果同时指定了状态筛选，进一步过滤
                if query_status and tasks:
                    status_labels = {
                        "undone": "未完成",
                        "done": "已完成",
                        "overdue": "逾期",
                    }
                    label = status_labels.get(query_status, query_status)
                    if query_status == "undone":
                        tasks = [t for t in tasks if not t.get("isDone", False)]
                    elif query_status == "done":
                        tasks = [t for t in tasks if t.get("isDone", False)]
                    elif query_status == "overdue":
                        now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
                        filtered = []
                        for t in tasks:
                            if t.get("isDone", False):
                                continue
                            due = t.get("dueDate", "")
                            if due:
                                try:
                                    due_dt = __import__('datetime').datetime.fromisoformat(due.replace("Z", "+00:00"))
                                    if due_dt < now:
                                        filtered.append(t)
                                except (ValueError, TypeError):
                                    pass
                        tasks = filtered
                    else:
                        # 按工作流状态名称筛选
                        if tasks:
                            try:
                                statuses = await tb.get_task_workflow_statuses(
                                    tasks[0].get("taskId") or tasks[0].get("_id", ""), sender_id
                                )
                                sid_map = {s["taskflowStatusId"]: s.get("name", "") for s in statuses if s.get("taskflowStatusId")}
                                for t in tasks:
                                    sid = t.get("taskflowstatusId") or t.get("taskflowStatusId", "")
                                    t["taskflowStatusName"] = sid_map.get(sid, "")
                            except Exception:
                                pass
                            tasks = [t for t in tasks if query_status in (t.get("taskflowStatusName", "") or "")]
                    title = f"迭代「{sprint_name}」{label}的任务"

            elif query_status:
                status_labels = {
                    "undone": "未完成",
                    "done": "已完成",
                    "overdue": "逾期",
                }
                label = status_labels.get(query_status, query_status)
                tasks = await tb.query_tasks_by_status(sender_id, query_status)
                title = f"{label}的任务"
            elif query_target == "all":
                tasks = await tb.query_project_tasks(sender_id)
                title = "项目全部任务"
            elif query_target == "me" or not query_target:
                tasks = await tb.query_user_tasks(sender_id)
                title = f"{sender_nick} 的任务"
            elif pr.target_task:
                # 查看特定任务详情
                task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
                if task:
                    detail = await tb.get_task(task["taskId"], sender_id)
                    if not tb._user_map:
                        await tb._load_project_members(sender_id)
                    # 注入工作流状态名
                    try:
                        statuses = await tb.get_task_workflow_statuses(task["taskId"], sender_id)
                        sid_map = {s["taskflowStatusId"]: s.get("name", "") for s in statuses if s.get("taskflowStatusId")}
                        detail["taskflowStatusName"] = sid_map.get(detail.get("taskflowstatusId", ""), "")
                    except Exception:
                        pass
                    md = self._format_task_detail(detail, tb)
                    self.reply_markdown("任务详情", md, msg)
                    return
                else:
                    self.reply_text(f"未找到任务『{pr.target_task}』。", msg)
                    return
            else:
                uid = await tb.resolve_user_id(query_target, operator_id=sender_id)
                if uid:
                    tasks = await tb.query_user_tasks(sender_id, target_user_id=uid)
                    title = f"{query_target} 的任务"
                else:
                    tasks = await tb.query_project_tasks(sender_id)
                    title = "项目全部任务"

            if not tasks:
                self.reply_text(f"{title}：暂无任务。", msg)
                return

            # 确保成员缓存已加载，用于 userId → 姓名显示
            if not tb._user_map:
                await tb._load_project_members(sender_id)

            # 预加载所有 executor 姓名（含缓存未命中的用户）
            executor_ids = [t.get("executorId", "") for t in tasks if t.get("executorId")]
            if executor_ids:
                await tb.ensure_user_names(executor_ids)

            # 如果任务没有 taskflowStatusName，尝试注入
            display_tasks = tasks[:20]
            if display_tasks and not display_tasks[0].get("taskflowStatusName"):
                try:
                    statuses = await tb.get_task_workflow_statuses(
                        display_tasks[0]["taskId"], sender_id
                    )
                    sid_map = {s["taskflowStatusId"]: s.get("name", "") for s in statuses if s.get("taskflowStatusId")}
                    for t in display_tasks:
                        sid = t.get("taskflowstatusId", "")
                        t["taskflowStatusName"] = sid_map.get(sid, "")
                except Exception:
                    pass

            md = self._format_task_list(title, display_tasks, tb)

            # 仅在用户明确要求通知时才发送私信
            should_notify = getattr(pr, 'notify', False)

            if should_notify:
                # 收集任务中的负责人 userId
                tasks_by_user: dict[str, list[dict]] = {}
                for t in display_tasks:
                    eid = t.get("executorId", "")
                    if eid and eid != sender_id:
                        tasks_by_user.setdefault(eid, []).append(t)

                if tasks_by_user:
                    # 群里显示已通知哪些人
                    notice_parts = []
                    for uid, utasks in tasks_by_user.items():
                        uname = tb.resolve_user_name(uid)
                        notice_parts.append(f"{uname}({len(utasks)}个)")
                    notice_line = "\n\n---\n\n✉️ 已向 " + "、".join(notice_parts) + " 发送任务通知"
                    md += notice_line
                    self.reply_markdown(title, md, msg)

                    # 通过机器人单聊发送私信通知（每人仅收到自己的任务）
                    await self._notify_assignees_via_dm(tb, tasks_by_user, title)
                else:
                    self.reply_markdown(title, md, msg)
            else:
                self.reply_markdown(title, md, msg)

        except Exception as e:
            self.logger.exception("查询任务失败")
            self.reply_text(f"查询任务失败: {str(e)}", msg)

    def _format_task_list(self, title: str, tasks: list[dict], tb=None) -> str:
        """格式化任务列表为 Markdown"""
        settings = get_settings()
        pid = settings.teambition_default_project_id
        vid = settings.teambition_default_view_id
        lines = [f"### {title} ({len(tasks)}个)"]
        for i, t in enumerate(tasks, 1):
            content = t.get("content", "未命名")
            is_done = t.get("isDone", False)
            priority = t.get("priority", 0)
            due = t.get("dueDate", "")
            executor_id = t.get("executorId", "")
            flow_status = t.get("taskflowStatusName", "")
            task_id = t.get("taskId", "")
            unique_id = t.get("uniqueId", 0)

            p_text = INT_TO_PRIORITY_TEXT.get(priority, "普通")
            status = "✓" if is_done else "○"
            due_text = f" | 截止: {due[:10]}" if due else ""

            # 执行者姓名
            executor_name = ""
            if executor_id and tb:
                executor_name = tb.resolve_user_name(executor_id)
            elif executor_id:
                executor_name = executor_id
            exec_text = f" | 负责人: {executor_name}" if executor_name else ""

            # 工作流状态
            flow_text = f" | {flow_status}" if flow_status else ""

            # 任务名称带跳转链接
            if task_id and pid and vid:
                link = f"https://www.teambition.com/project/{pid}/tasks/view/{vid}/task/{task_id}"
                content_text = f"[{content}]({link})"
            else:
                content_text = content

            lines.append(f"{i}. {status} **{content_text}** [{p_text}]{exec_text}{flow_text}{due_text}")

            # 提交代码
            if tb and task_id:
                submit_code = tb.format_submit_code(task_id, unique_id, content, executor_name)
                if submit_code:
                    lines.append(f"    `提交代码: {submit_code}`")

        return "\n\n".join(lines)

    def _format_task_detail(self, task: dict, tb=None) -> str:
        """格式化单个任务详情"""
        settings = get_settings()
        pid = settings.teambition_default_project_id
        content = task.get("content", "未命名")
        is_done = task.get("isDone", False)
        priority = task.get("priority", 0)
        due = task.get("dueDate", "未设置")
        start = task.get("startDate", "未设置")
        note = task.get("note", "无")
        executor_id = task.get("executorId", "")
        created = task.get("created", "")
        flow_status = task.get("taskflowStatusName", "")
        task_id = task.get("taskId", "")
        unique_id = task.get("uniqueId", 0)

        p_text = INT_TO_PRIORITY_TEXT.get(priority, "普通")
        done_text = "已完成 ✓" if is_done else "未完成"
        status_text = flow_status if flow_status else done_text

        # 执行者姓名
        executor_name = "未指定"
        if executor_id and tb:
            executor_name = tb.resolve_user_name(executor_id)
        elif executor_id:
            executor_name = executor_id

        # 跳转链接
        vid = settings.teambition_default_view_id
        if task_id and pid and vid:
            link = f"https://www.teambition.com/project/{pid}/tasks/view/{vid}/task/{task_id}"
            link_text = f"[点击查看]({link})"
        else:
            link_text = ""

        lines = [
            f"### 任务详情: {content}",
            f"**状态:** {status_text}",
            f"**优先级:** {p_text}",
            f"**负责人:** {executor_name}",
            f"**截止日期:** {due[:10] if due and due != '未设置' else due}",
            f"**开始日期:** {start[:10] if start and start != '未设置' else start}",
            f"**备注:** {note or '无'}",
            f"**创建时间:** {created[:10] if created else '未知'}",
        ]
        if link_text:
            lines.append(f"\n🔗 {link_text}")

        # 提交代码
        if tb and task_id:
            submit_code = tb.format_submit_code(task_id, unique_id, content, executor_name)
            if submit_code:
                lines.append(f"\n`提交代码: {submit_code}`")

        return "\n\n".join(lines)

    # ==============================================================
    # 设置工作流状态
    # ==============================================================

    async def _handle_status(self, pr, msg, sender_id, sender_nick):
        if not pr.target_task:
            self.reply_text("请告诉我要设置哪个任务的状态。", msg)
            return
        if not pr.status_name:
            self.reply_text(f"请告诉我要把任务『{pr.target_task}』设为什么状态。", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id)
        if not task:
            self.reply_text(f"未找到任务『{pr.target_task}』。", msg)
            return

        try:
            await tb.set_task_status_by_name(task["taskId"], sender_id, pr.status_name)
            self.reply_markdown(
                "状态已更新",
                f"### 任务状态已更新\n\n**任务:** {task.get('content', pr.target_task)}\n\n**新状态:** {pr.status_name}\n\n🔗 {self._task_link_md(task['taskId'])}",
                msg,
            )
        except Exception as e:
            self.logger.exception("设置任务状态失败")
            self.reply_text(f"设置状态失败: {str(e)}", msg)

    # ==============================================================
    # 帮助
    # ==============================================================

    async def _handle_help(self, pr, msg, sender_id, sender_nick):
        self.reply_markdown("帮助", HELP_TEXT, msg)


def start_fastapi_server():
    """在后台线程中启动 FastAPI (用于接收 Teambition Webhook)"""
    import uvicorn
    from main import app

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")


def main():
    settings = get_settings()

    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        logger.error("请配置 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("钉钉 Teambition 任务机器人 (Stream 模式)")
    logger.info("=" * 50)

    # 启动 FastAPI (后台线程, 用于 Teambition Webhook)
    fastapi_thread = threading.Thread(target=start_fastapi_server, daemon=True)
    fastapi_thread.start()
    logger.info("FastAPI 服务已启动 (端口 8001, 用于 Teambition Webhook)")

    # 启动钉钉 Stream 客户端
    credential = dingtalk_stream.Credential(
        settings.dingtalk_app_key,
        settings.dingtalk_app_secret,
    )
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        TaskBotHandler(logger),
    )

    logger.info("钉钉 Stream 连接启动中...")
    logger.info("机器人已就绪，在钉钉群中 @机器人 发送消息即可")
    client.start_forever()


if __name__ == "__main__":
    main()

