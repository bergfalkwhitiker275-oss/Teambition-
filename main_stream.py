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
import msvcrt
import pathlib
import re
import sys
import threading
import time

import httpx
import requests
import dingtalk_stream
from dingtalk_stream import AckMessage

from config import get_settings
from teambition.client import get_teambition_client
from llm.parser import parse_task_from_message, format_missing_info_message, reset_to_primary, switch_to_fallback, is_using_fallback

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
- "创建一个需求《用户登录模块》给张三"
- "提一个Bug：页面加载异常，负责人李四"
- "给吕鑫创建美术任务：角色立绘"

**修改任务:**
- "把首页设计的优先级改为高"
- "把首页设计的截止日期改到下周三"
- "把首页设计的负责人改为庄健男"
- "给首页设计添加备注：需要参考竞品"
- "把吕鑫加为首页设计的参与者"
- "把首页设计的类型改为缺陷"

**批量修改类型:**
- "将蔡宇航的工单类型都改为美术"
- "把所有吕鑫的任务改为需求类型"

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

**系统管理 (仅管理员):**
- "恢复主模型" — 主模型配额恢复后，重置回主模型
- "切换备用模型" — 手动切换到备用模型
"""


# 用户级附件暂存：{sender_id: {"attachments": [...], "timestamp": float}}
# 文件/图片消息无法与文字合并发送时，先暂存附件，等下一条文字指令自动关联
PENDING_ATTACHMENTS_TTL = 300  # 暂存有效期 5 分钟


class TaskBotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉机器人消息处理器 - Stream 模式"""

    _MAP_FILE = pathlib.Path(__file__).parent / ".task_id_map.json"

    def __init__(self, logger: logging.Logger = None):
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        if logger:
            self.logger = logger
        self._pending_attachments: dict[str, dict] = {}
        self._task_id_map: dict[str, str] = self._load_task_id_map()

    def _load_task_id_map(self) -> dict:
        try:
            return json.loads(self._MAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_task_id_map(self) -> None:
        try:
            self._MAP_FILE.write_text(json.dumps(self._task_id_map), encoding="utf-8")
        except Exception as e:
            self.logger.warning("保存 task_id_map 失败: %s", e)

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
            # 临时: 打印原始 callback.data 以便分析引用消息结构
            self.logger.debug("RAW callback.data: %s", callback.data)
            # sender_staff_id 是组织内真实 userId，sender_id 是加密格式不可用
            sender_id = incoming_message.sender_staff_id or incoming_message.sender_id
            sender_nick = incoming_message.sender_nick
            conversation_type = incoming_message.conversation_type

            # 提取文本和附件（支持 text / richText / picture / file / video 消息类型）
            text = ""
            # attachment_infos: [{"download_code": ..., "file_name": ..., "file_type": ...}, ...]
            attachment_infos = []
            msg_type = incoming_message.message_type or ""
            if msg_type == "richText":
                text_parts = incoming_message.get_text_list() or []
                text = "\n".join(text_parts).strip()
                for code in (incoming_message.get_image_list() or []):
                    attachment_infos.append({"download_code": code, "file_name": "image.png", "file_type": "image/png"})
            elif msg_type == "picture":
                for code in (incoming_message.get_image_list() or []):
                    attachment_infos.append({"download_code": code, "file_name": "image.png", "file_type": "image/png"})
            elif msg_type == "file":
                # SDK 未原生解析 file 类型，数据在 extensions['content'] 中
                file_content = incoming_message.extensions.get("content", {})
                dl_code = file_content.get("downloadCode", "")
                fname = file_content.get("fileName", "attachment")
                if dl_code:
                    # 根据文件扩展名推断 MIME 类型
                    import mimetypes
                    ftype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
                    attachment_infos.append({"download_code": dl_code, "file_name": fname, "file_type": ftype})
            elif msg_type == "video":
                video_content = incoming_message.extensions.get("content", {})
                dl_code = video_content.get("downloadCode", "")
                video_type = video_content.get("videoType", "mp4")
                if dl_code:
                    attachment_infos.append({"download_code": dl_code, "file_name": f"video.{video_type}", "file_type": f"video/{video_type}"})
            if incoming_message.text:
                text = incoming_message.text.content.strip()
                # 引用消息处理: 把被引用消息的文本注入上下文
                # 钉钉引用消息结构: text.isReplyMsg=True, text.repliedMsg.content.text = 被引用内容
                replied_msg = incoming_message.text.extensions.get("repliedMsg")
                if replied_msg and isinstance(replied_msg, dict):
                    # 打印完整结构，用于分析钉钉引用消息的实际字段
                    self.logger.info("repliedMsg 完整结构: %s", json.dumps(replied_msg, ensure_ascii=False))
                    replied_content = replied_msg.get("content", {})
                    if isinstance(replied_content, dict):
                        replied_text = replied_content.get("text", "").strip()
                    else:
                        replied_text = str(replied_content).strip()
                    if replied_text:
                        id_match = re.search(r'任务创建成功\s+(\S+)', replied_text) or \
                                   re.search(r'\*\*任务ID[:：]\*\*\s*(\S+)', replied_text)
                        if id_match:
                            task_label = id_match.group(1).strip()
                            text = f"[引用任务ID: {task_label}] {text}"
                            self.logger.info("引用消息提取任务ID: %s", task_label)
                        else:
                            task_match = re.search(r'\*\*任务[:：]\*\*\s*(.+)', replied_text)
                            if task_match:
                                text = f"[引用任务: {task_match.group(1).strip()}] {text}"
                                self.logger.info("引用消息提取任务名: %s", task_match.group(1).strip())
                            else:
                                text = f"[引用消息: {replied_text[:200]}] {text}"
                                self.logger.info("引用消息原文注入: %s", replied_text[:100])
            elif msg_type in ("file", "video") and not text:
                # 纯文件/视频消息无文本，从前一条或提示用户
                pass

            # 预处理: 清理钎钎 @提及格式，如 "@许乃轩(许乃轩)" -> "许乃轩"
            text = re.sub(r'@([^@\(\)]+?)\(\1\)', r'\1', text)
            # 处理 "@许乃轩" (无括号) -> "许乃轩"
            text = re.sub(r'@([\u4e00-\u9fa5a-zA-Z0-9_]+)', r'\1', text)
            text = text.strip()

            # 预处理: 钎钎 SDK 会把 @提及的人名从 text.content 中剥离，
            # 需要从 at_users 获取被 @的用户 staffId，查出姓名后重新注入文本
            at_user_names = []
            if incoming_message.at_users:
                for at_user in incoming_message.at_users:
                    staff_id = at_user.staff_id
                    if not staff_id:
                        continue  # 跳过机器人自身（无 staffId）
                    if staff_id == sender_id:
                        continue  # 跳过发送者自己
                    try:
                        tb = get_teambition_client()
                        token = await tb._ensure_token()
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.post(
                                "https://oapi.dingtalk.com/topapi/v2/user/get",
                                params={"access_token": token},
                                json={"userid": staff_id},
                            )
                            data = resp.json()
                            if data.get("errcode") == 0:
                                name = data["result"].get("name", "")
                                if name:
                                    at_user_names.append(name)
                            else:
                                self.logger.warning("@用户解析失败 staffId=%s: %s", staff_id, data.get("errmsg"))
                    except Exception as e:
                        self.logger.warning("解析@用户名称失败 (staffId=%s): %s", staff_id, e)

            if at_user_names:
                names_str = "、".join(at_user_names)
                # 把人名注入到文本中
                if re.match(r'^\u7ed9\s', text):
                    # "给  下一个单子" -> "给许乃轩、王雅菲下一个单子"
                    text = re.sub(r'^\u7ed9\s+', f'给{names_str}', text)
                else:
                    text = f"[提及用户: {names_str}] {text}"
                self.logger.info("注入@用户名称: %s, 处理后文本: %s", at_user_names, text)

            self.logger.info(
                "收到消息: sender=%s (staffId=%s), type=%s, text=%s, attachments=%d",
                sender_nick, sender_id, conversation_type, text, len(attachment_infos),
            )

            if not text:
                if attachment_infos:
                    # 暂存附件，等待后续文字指令
                    existing = self._pending_attachments.get(sender_id, {}).get("attachments", [])
                    existing.extend(attachment_infos)
                    self._pending_attachments[sender_id] = {
                        "attachments": existing,
                        "timestamp": time.time(),
                    }
                    count = len(existing)
                    self.reply_text(
                        f"已暂存 {count} 个附件，请继续发送文字指令创建任务，附件将自动关联。\n（暂存有效期 5 分钟）",
                        incoming_message,
                    )
                else:
                    self.reply_text("请发送任务相关指令，发送 \"帮助\" 查看支持的操作。", incoming_message)
                return AckMessage.STATUS_OK, "OK"

            # 合并暂存的附件（有效期内）
            pending = self._pending_attachments.pop(sender_id, None)
            if pending and (time.time() - pending["timestamp"]) < PENDING_ATTACHMENTS_TTL:
                attachment_infos = pending["attachments"] + attachment_infos
                self.logger.info("合并暂存附件: %d 个 (来自用户 %s)", len(pending["attachments"]), sender_id)

            # 系统管理命令不经过 LLM（LLM 不可用时也能执行）
            _cmd = text.strip()
            self.logger.info("命令匹配检查: repr=%r", _cmd)
            if _cmd in ("恢复主模型", "切换主模型"):
                await self._handle_reset_primary(None, incoming_message, sender_id, sender_nick)
                return AckMessage.STATUS_OK, "OK"

            if _cmd == "切换备用模型":
                await self._handle_switch_fallback(None, incoming_message, sender_id, sender_nick)
                return AckMessage.STATUS_OK, "OK"

            # 1. LLM 解析意图
            parse_result = await parse_task_from_message(text)

            # 2. 根据意图分发
            action = parse_result.action
            handler_map = {
                "create": self._handle_create,
                "batch_create": self._handle_batch_create,
                "update": self._handle_update,
                "batch_update_type": self._handle_batch_update_type,
                "complete": self._handle_complete,
                "reopen": self._handle_reopen,
                "delete": self._handle_delete,
                "query": self._handle_query,
                "status": self._handle_status,
                "export_submit_code": self._handle_export_submit_code,
                "help": self._handle_help,
                "reset_primary": self._handle_reset_primary,
            }

            handler = handler_map.get(action)
            if handler:
                await handler(parse_result, incoming_message, sender_id, sender_nick,
                              attachment_infos=attachment_infos)
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

    async def _handle_create(self, pr, msg, sender_id, sender_nick, attachment_infos=None):
        if not pr.is_valid_for_create:
            missing_msg = format_missing_info_message(pr)
            self.reply_text(missing_msg, msg)
            return

        if not sender_id:
            self.reply_text("无法获取你的用户ID，请确认机器人权限配置正确。", msg)
            return

        tb = get_teambition_client()

        # 预加载项目前缀（用于生成 BP3-51 格式的任务ID）
        await tb.get_project_key(sender_id)

        # 解析负责人：未指定时默认为发送者自己
        assignee_id = None
        assignee_name = pr.assignee or sender_nick
        if pr.assignee:
            assignee_id = await tb.resolve_user_id(pr.assignee, operator_id=sender_id)
            if not assignee_id:
                self.reply_text(f"❌ 创建工单失败：未找到项目成员「{pr.assignee}」，请确认姓名是否正确。", msg)
                return
        else:
            # 未指定负责人，默认为发送者
            assignee_id = sender_id
            assignee_name = sender_nick

        priority = PRIORITY_TO_INT.get(pr.priority or "medium", 0) if pr.priority else None

        # 解析任务类型
        scenario_field_config_id = None
        task_type_name = pr.task_type or "需求"
        try:
            scenario_field_config_id = await tb.resolve_scenario_field_config_id(
                task_type_name, operator_id=sender_id
            )
            if not scenario_field_config_id:
                self.logger.warning("未找到任务类型'%s'，将使用默认类型创建", task_type_name)
        except Exception as e:
            self.logger.error("解析任务类型失败: %s", e)

        try:
            result = await tb.create_task(
                title=pr.title,
                operator_id=sender_id,
                assignee_id=assignee_id,
                due_date=pr.due_date,
                priority=priority,
                note=pr.note or f"由 {sender_nick} 通过钉钉机器人创建",
                scenario_field_config_id=scenario_field_config_id,
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
                sprint_id, sprint_actual = await tb.resolve_sprint_id(pr.sprint, sender_id)
                if sprint_id:
                    try:
                        await tb.update_task_sprint(task_id, sender_id, sprint_id)
                        sprint_text = sprint_actual
                    except Exception as e:
                        self.logger.error("创建任务时设置迭代失败: %s", e)
                        sprint_text = f"设置失败"
                else:
                    sprint_text = f"未找到『{pr.sprint}』"

            # 如果有需求来源或验收人，设置自定义字段
            requirement_source_text = ""
            acceptor_text = ""
            if task_id and (pr.requirement_source or pr.acceptor):
                if pr.requirement_source:
                    ok = await tb.set_task_custom_field_by_name(
                        task_id, sender_id, "需求来源", pr.requirement_source
                    )
                    requirement_source_text = pr.requirement_source if ok else f"设置失败"
                if pr.acceptor:
                    ok = await tb.set_task_custom_field_by_name(
                        task_id, sender_id, "验收人", pr.acceptor
                    )
                    acceptor_text = pr.acceptor if ok else f"设置失败"

            priority_text = PRIORITY_TO_TEXT.get(pr.priority, "普通") if pr.priority else "普通"

            # 上传附件（如果用户发送了图片/文件/压缩包）
            attachment_count = 0
            if attachment_infos and task_id:
                for idx, att in enumerate(attachment_infos):
                    try:
                        download_url = self.get_image_download_url(att["download_code"])
                        if not download_url:
                            self.logger.warning("获取附件下载链接失败: %s", att["file_name"])
                            continue
                        async with httpx.AsyncClient(timeout=60.0) as dl_client:
                            dl_resp = await dl_client.get(download_url)
                            dl_resp.raise_for_status()
                            file_bytes = dl_resp.content
                        # 使用原始文件名，多个同名文件加序号
                        file_name = att["file_name"]
                        if len(attachment_infos) > 1 and file_name == "image.png":
                            ext = file_name.rsplit(".", 1)
                            file_name = f"{ext[0]}_{idx + 1}.{ext[1]}" if len(ext) == 2 else f"{file_name}_{idx + 1}"
                        content_type = dl_resp.headers.get("content-type", att["file_type"])
                        await tb.upload_attachment_to_task(
                            task_id, sender_id, file_name, file_bytes, content_type,
                        )
                        attachment_count += 1
                        self.logger.info("附件上传成功: %s -> taskId=%s", file_name, task_id)
                    except Exception as e:
                        self.logger.error("附件上传失败 (%s): %s", att["file_name"], e)

            md = [
                "### 任务创建成功",
                f"**任务:** {pr.title}",
                f"**类型:** {task_type_name}",
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
            if requirement_source_text:
                md.append(f"**需求来源:** {requirement_source_text}")
            if acceptor_text:
                md.append(f"**验收人:** {acceptor_text}")
            if attachment_count > 0:
                md.append(f"**附件:** 已上传 {attachment_count} 个")
            link_md = self._task_link_md(task_id)
            if link_md:
                md.append(f"\n🔗 {link_md}")

            # 提交代码
            unique_id = result.get("uniqueId", 0)
            submit_code = tb.format_submit_code(task_id, unique_id, pr.title, assignee_name)
            if submit_code:
                md.append(f"\n**提交代码:** ⬇️ 见下条消息，可右键直接复制")

            task_label = self._finalize_task_reply(md, tb, task_id, unique_id, msg)
            self.logger.info("任务创建成功: taskId=%s uniqueId=%s label=%s", task_id, unique_id, task_label)

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
    # 批量创建任务
    # ==============================================================

    async def _handle_batch_create(self, pr, msg, sender_id, sender_nick, attachment_infos=None):
        if not pr.is_valid_for_batch_create:
            self.reply_text("请提供要创建的任务列表，例如：\n\"帮我提这几个单子：\nXXX\nYYY\nZZZ\"", msg)
            return

        tb = get_teambition_client()
        await tb.get_project_key(sender_id)

        # 预解析公共属性
        common_sprint_id = None
        common_sprint_name = pr.sprint
        if pr.sprint:
            common_sprint_id, common_sprint_name = await tb.resolve_sprint_id(pr.sprint, sender_id)
            if not common_sprint_id:
                self.logger.warning("未找到迭代 '%s'", pr.sprint)

        common_assignee_id = sender_id
        common_assignee_name = sender_nick
        if pr.assignee:
            uid = await tb.resolve_user_id(pr.assignee, operator_id=sender_id)
            if uid:
                common_assignee_id = uid
                common_assignee_name = pr.assignee

        common_type_name = pr.task_type or "需求"
        common_sfc_id = None
        try:
            common_sfc_id = await tb.resolve_scenario_field_config_id(common_type_name, sender_id)
        except Exception as e:
            self.logger.error("解析公共任务类型失败: %s", e)

        common_priority = PRIORITY_TO_INT.get(pr.priority or "medium", 0) if pr.priority else None

        # 逐个创建任务，每条独立回复（格式与单独创建一致）
        success_count = 0
        fail_count = 0
        for task_item in pr.tasks:
            title = task_item.get("title", "")
            if not title:
                continue

            # 独立属性覆盖公共属性
            assignee_id = common_assignee_id
            assignee_name = common_assignee_name
            if task_item.get("assignee"):
                uid = await tb.resolve_user_id(task_item["assignee"], operator_id=sender_id)
                if uid:
                    assignee_id = uid
                    assignee_name = task_item["assignee"]
                else:
                    self.reply_text(f"❌ 任务「{title}」创建失败：未找到项目成员「{task_item['assignee']}」。", msg)
                    fail_count += 1
                    continue

            sfc_id = common_sfc_id
            task_type_name = task_item.get("task_type") or common_type_name
            if task_item.get("task_type") and task_item["task_type"] != common_type_name:
                try:
                    sfc_id = await tb.resolve_scenario_field_config_id(task_type_name, sender_id) or common_sfc_id
                except Exception:
                    pass

            priority = common_priority
            if task_item.get("priority"):
                priority = PRIORITY_TO_INT.get(task_item["priority"], 0)

            due_date = task_item.get("due_date") or pr.due_date
            note = task_item.get("note") or pr.note or f"由 {sender_nick} 通过钉钉机器人批量创建"

            try:
                result = await tb.create_task(
                    title=title,
                    operator_id=sender_id,
                    assignee_id=assignee_id,
                    due_date=due_date,
                    priority=priority,
                    note=note,
                    scenario_field_config_id=sfc_id,
                )
                task_id = result.get("taskId", "")

                # 设置开始日期
                start_date = task_item.get("start_date") or pr.start_date
                if start_date and task_id:
                    try:
                        await tb.update_task_start_date(task_id, sender_id, start_date)
                    except Exception as e:
                        self.logger.error("批量创建设置开始日期失败 %s: %s", title, e)

                # 设置迭代
                sprint_id = common_sprint_id
                sprint_text = common_sprint_name or ""
                if task_item.get("sprint"):
                    item_sprint_id, item_sprint_name = await tb.resolve_sprint_id(task_item["sprint"], sender_id)
                    sprint_id = item_sprint_id or common_sprint_id
                    sprint_text = item_sprint_name if item_sprint_id else sprint_text
                if sprint_id and task_id:
                    try:
                        await tb.update_task_sprint(task_id, sender_id, sprint_id)
                    except Exception as e:
                        self.logger.error("批量创建设置迭代失败 %s: %s", title, e)

                # 设置需求来源和验收人自定义字段
                requirement_source_text = ""
                acceptor_text = ""
                if task_id and (pr.requirement_source or pr.acceptor):
                    if pr.requirement_source:
                        ok = await tb.set_task_custom_field_by_name(
                            task_id, sender_id, "需求来源", pr.requirement_source
                        )
                        requirement_source_text = pr.requirement_source if ok else "设置失败"
                    if pr.acceptor:
                        ok = await tb.set_task_custom_field_by_name(
                            task_id, sender_id, "验收人", pr.acceptor
                        )
                        acceptor_text = pr.acceptor if ok else "设置失败"

                # 上传附件（如果用户发送了图片/文件/压缩包）
                attachment_count = 0
                if attachment_infos and task_id:
                    for idx, att in enumerate(attachment_infos):
                        try:
                            download_url = self.get_image_download_url(att["download_code"])
                            if not download_url:
                                continue
                            async with httpx.AsyncClient(timeout=60.0) as dl_client:
                                dl_resp = await dl_client.get(download_url)
                                dl_resp.raise_for_status()
                                file_bytes = dl_resp.content
                            file_name = att["file_name"]
                            if len(attachment_infos) > 1 and file_name == "image.png":
                                ext = file_name.rsplit(".", 1)
                                file_name = f"{ext[0]}_{idx + 1}.{ext[1]}" if len(ext) == 2 else f"{file_name}_{idx + 1}"
                            content_type = dl_resp.headers.get("content-type", att["file_type"])
                            await tb.upload_attachment_to_task(
                                task_id, sender_id, file_name, file_bytes, content_type,
                            )
                            attachment_count += 1
                        except Exception as e:
                            self.logger.error("批量创建附件上传失败 %s (%s): %s", title, att["file_name"], e)

                # --- 逐条回复，格式与单独创建一致 ---
                priority_text = PRIORITY_TO_TEXT.get(priority, "普通") if priority else "普通"
                md = [
                    "### 任务创建成功",
                    f"**任务:** {title}",
                    f"**类型:** {task_type_name}",
                    f"**负责人:** {assignee_name}",
                    f"**截止日期:** {due_date or '未设置'}",
                    f"**优先级:** {priority_text}",
                ]
                start_date = task_item.get("start_date") or pr.start_date
                if start_date:
                    md.append(f"**开始日期:** {start_date}")
                if note and not note.startswith("由 "):
                    md.append(f"**备注:** {note}")
                if sprint_text:
                    md.append(f"**迭代:** {sprint_text}")
                if requirement_source_text:
                    md.append(f"**需求来源:** {requirement_source_text}")
                if acceptor_text:
                    md.append(f"**验收人:** {acceptor_text}")
                if attachment_count > 0:
                    md.append(f"**附件:** 已上传 {attachment_count} 个")
                link_md = self._task_link_md(task_id)
                if link_md:
                    md.append(f"\n🔗 {link_md}")

                unique_id = result.get("uniqueId", 0)
                submit_code = tb.format_submit_code(task_id, unique_id, title, assignee_name)
                if submit_code:
                    md.append(f"\n**提交代码:** ⬇️ 见下条消息，可右键直接复制")

                self._finalize_task_reply(md, tb, task_id, unique_id, msg)

                if submit_code:
                    self.reply_text(submit_code, msg)

                # 通知项目管理员
                if task_id:
                    await self._notify_admins_new_task(
                        tb, task_id, title, assignee_name, sender_nick, sender_id,
                        sprint_text=sprint_text,
                        due_date=due_date,
                    )

                success_count += 1
                self.logger.info("批量创建任务成功: %s (taskId=%s)", title, task_id)
            except Exception as e:
                self.logger.error("批量创建任务失败 %s: %s", title, e)
                fail_count += 1
                self.reply_text(f"创建任务失败: {title}\n原因: {str(e)}", msg)

        self.logger.info("批量创建任务: total=%d, success=%d, fail=%d",
                         len(pr.tasks), success_count, fail_count)

    # ==============================================================
    # 导出迭代提交代码
    # ==============================================================

    async def _handle_export_submit_code(self, pr, msg, sender_id, sender_nick, **kwargs):
        sprint_name = pr.sprint or pr.query_sprint
        if not sprint_name:
            self.reply_text("请指定迭代名称，例如：\n\"导出蚩梦觉醒迭代的提交代码\"", msg)
            return

        tb = get_teambition_client()
        await tb.get_project_key(sender_id)

        sprint_id, sprint_actual = await tb.resolve_sprint_id(sprint_name, sender_id)
        if not sprint_id:
            self.reply_text(f"未找到迭代『{sprint_name}』，请检查迭代名称。", msg)
            return

        self.reply_text(f"正在导出迭代「{sprint_actual}」的提交代码，请稍候...", msg)

        tasks = await tb.query_tasks_by_sprint(sprint_id, sender_id)
        if not tasks:
            self.reply_text(f"迭代「{sprint_actual}」下暂无任务。", msg)
            return

        # 确保成员缓存
        if not tb._user_map:
            await tb._load_project_members(sender_id)
        executor_ids = [t.get("executorId", "") for t in tasks if t.get("executorId")]
        if executor_ids:
            await tb.ensure_user_names(executor_ids)

        # 生成提交代码列表
        lines = []
        for t in tasks:
            task_id = t.get("taskId", "")
            unique_id = t.get("uniqueId", 0)
            content = t.get("content", "")
            executor_id = t.get("executorId", "")
            executor_name = tb.resolve_user_name(executor_id) if executor_id else ""
            code = tb.format_submit_code(task_id, unique_id, content, executor_name)
            if code:
                lines.append(code)

        if not lines:
            self.reply_text(f"迭代「{sprint_actual}」下的任务暂无可导出的提交代码。", msg)
            return

        # 以纯文本发送，每行一条，方便用户一键复制
        header = f"迭代「{sprint_actual}」共 {len(lines)} 条提交代码：\n\n"
        self.reply_text(header + "\n".join(lines), msg)
        self.logger.info("导出提交代码: sprint=%s, count=%d", sprint_actual, len(lines))

    # ==============================================================
    # 修改任务
    # ==============================================================

    async def _handle_update(self, pr, msg, sender_id, sender_nick, **kwargs):
        if not pr.target_task:
            self.reply_text("请告诉我要修改哪个任务，例如：\n\"把任务'首页设计'的优先级改为高\"", msg)
            return
        if not pr.update_fields:
            self.reply_text(f"请告诉我要修改任务『{pr.target_task}』的什么属性。\n支持: 优先级/负责人/截止日期/开始日期/标题/备注/参与者", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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
                sprint_id, sprint_actual = await tb.resolve_sprint_id(sprint_name, sender_id)
                if sprint_id:
                    await tb.update_task_sprint(task_id, sender_id, sprint_id)
                    updated.append(f"**迭代:** {sprint_actual}")
                else:
                    updated.append(f"**迭代:** 未找到『{sprint_name}』，跳过")

            if "task_type" in fields:
                type_name = fields["task_type"]
                config_id = await tb.resolve_scenario_field_config_id(type_name, sender_id)
                if config_id:
                    await tb.update_task_scenario_field_config(task_id, sender_id, config_id)
                    updated.append(f"**类型:** {type_name}")
                else:
                    updated.append(f"**类型:** 未找到『{type_name}』，跳过")

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
    # 批量修改任务类型
    # ==============================================================

    async def _handle_batch_update_type(self, pr, msg, sender_id, sender_nick, **kwargs):
        target_user = pr.batch_target_user
        new_type = pr.batch_new_type

        if not target_user:
            self.reply_text("请告诉我要修改谁的任务类型，例如：\n\"将蔡宇航的工单类型都改为美术\"", msg)
            return
        if not new_type:
            self.reply_text(f"请告诉我要把{target_user}的任务改为什么类型。\n支持: 需求/任务/缺陷/美术", msg)
            return

        tb = get_teambition_client()

        # 解析目标用户
        user_id = await tb.resolve_user_id(target_user, operator_id=sender_id)
        if not user_id:
            self.reply_text(f"未找到项目成员『{target_user}』。", msg)
            return

        # 解析目标任务类型
        config_id = await tb.resolve_scenario_field_config_id(new_type, sender_id)
        if not config_id:
            self.reply_text(f"未找到任务类型『{new_type}』，请确认类型名称是否正确。", msg)
            return

        # 查询该用户的所有任务
        tasks = await tb.query_user_tasks(sender_id, target_user_id=user_id, max_results=200)
        if not tasks:
            self.reply_text(f"{target_user}暂无任务。", msg)
            return

        # 批量更新
        success_count = 0
        fail_count = 0
        for t in tasks:
            task_id = t.get("taskId", "")
            if not task_id:
                continue
            try:
                await tb.update_task_scenario_field_config(task_id, sender_id, config_id)
                success_count += 1
            except Exception as e:
                self.logger.error("批量更新任务类型失败 taskId=%s: %s", task_id, e)
                fail_count += 1

        md = [
            "### 批量修改任务类型完成",
            f"**目标用户:** {target_user}",
            f"**新类型:** {new_type}",
            f"**成功:** {success_count} 个",
        ]
        if fail_count:
            md.append(f"**失败:** {fail_count} 个")

        self.reply_markdown("批量修改完成", "\n\n".join(md), msg)
        self.logger.info("批量更新任务类型: user=%s, type=%s, success=%d, fail=%d",
                         target_user, new_type, success_count, fail_count)

    # ==============================================================
    # 完成任务
    # ==============================================================

    async def _handle_complete(self, pr, msg, sender_id, sender_nick, **kwargs):
        if not pr.target_task:
            self.reply_text("请告诉我要完成哪个任务，例如：\"把首页设计标记为完成\"", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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

    async def _handle_reopen(self, pr, msg, sender_id, sender_nick, **kwargs):
        if not pr.target_task:
            self.reply_text("请告诉我要重新打开哪个任务。", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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

    async def _handle_delete(self, pr, msg, sender_id, sender_nick, **kwargs):
        if not pr.target_task:
            self.reply_text("请告诉我要删除哪个任务。", msg)
            return

        tb = get_teambition_client()

        # 权限校验: 只有项目管理员才能删除工单
        admin_ids = await tb.get_project_admins(sender_id)
        if sender_id not in admin_ids:
            self.reply_text("⚠️ 仅项目管理员可以删除工单，您没有删除权限。", msg)
            self.logger.warning("非管理员尝试删除任务: sender=%s (%s)", sender_nick, sender_id)
            return

        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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
            self.logger.info("任务已删除: %s (by admin %s)", task["taskId"], sender_nick)
        except Exception as e:
            self.logger.exception("删除任务失败")
            self.reply_text(f"删除任务失败: {str(e)}", msg)

    # ==============================================================
    # 查询任务
    # ==============================================================

    async def _handle_query(self, pr, msg, sender_id, sender_nick, **kwargs):
        tb = get_teambition_client()
        await tb.get_project_key(sender_id)
        query_target = pr.query_target
        query_status = pr.query_status
        query_sprint = getattr(pr, 'query_sprint', None)

        try:
            # 如果指定了迭代，先解析 sprintId
            sprint_id = None
            sprint_name = None
            if query_sprint:
                sprint_id, sprint_name = await tb.resolve_sprint_id(query_sprint, sender_id)
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
                task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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

    async def _handle_status(self, pr, msg, sender_id, sender_nick, **kwargs):
        if not pr.target_task:
            self.reply_text("请告诉我要设置哪个任务的状态。", msg)
            return
        if not pr.status_name:
            self.reply_text(f"请告诉我要把任务『{pr.target_task}』设为什么状态。", msg)
            return

        tb = get_teambition_client()
        task = await tb.search_task_by_title(pr.target_task, operator_id=sender_id, task_id_map=self._task_id_map)
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

    async def _handle_help(self, pr, msg, sender_id, sender_nick, **kwargs):
        self.reply_markdown("帮助", HELP_TEXT, msg)

    def _finalize_task_reply(self, md: list, tb, task_id: str, unique_id: int, msg) -> str:
        """追加任务ID到回复正文并发送，返回 task_label（如 BP3-108）"""
        project_key = tb._project_key or ""
        task_label = f"{project_key}-{unique_id}" if (project_key and unique_id) else ""
        if task_label:
            md.append(f"\n**任务ID:** {task_label}")
        reply_title = f"任务创建成功 {task_label}" if task_label else "任务创建成功"
        self.reply_markdown(reply_title, "\n\n".join(md), msg)
        if task_label:
            self._task_id_map[task_label] = task_id
            self._save_task_id_map()
        return task_label

    # ==============================================================
    # 系统管理
    # ==============================================================

    async def _handle_reset_primary(self, pr, msg, sender_id, sender_nick, **kwargs):
        """重置 LLM 为主模型（仅项目管理员可操作）"""
        tb = get_teambition_client()
        admin_ids = await tb.get_project_admins(sender_id)
        if sender_id not in admin_ids:
            self.reply_text("⚠️ 仅项目管理员可以重置 LLM 模型。", msg)
            return
        was_fallback = is_using_fallback()
        reset_to_primary()
        if was_fallback:
            self.reply_text("✅ 已重置为主模型，下一条消息将使用主模型处理。", msg)
            self.logger.info("管理员 %s (%s) 手动重置为主模型", sender_nick, sender_id)
        else:
            self.reply_text("当前已在使用主模型，无需重置。", msg)

    async def _handle_switch_fallback(self, pr, msg, sender_id, sender_nick, **kwargs):
        """手动切换到备用模型（仅项目管理员可操作）"""
        tb = get_teambition_client()
        admin_ids = await tb.get_project_admins(sender_id)
        if sender_id not in admin_ids:
            self.reply_text("⚠️ 仅项目管理员可以切换 LLM 模型。", msg)
            return
        settings = get_settings()
        if not settings.llm_fallback_model:
            self.reply_text("⚠️ 未配置备用模型（LLM_FALLBACK_MODEL），无法切换。", msg)
            return
        was_fallback = is_using_fallback()
        switch_to_fallback()
        if not was_fallback:
            self.reply_text(
                f"✅ 已切换到备用模型：{settings.llm_fallback_model}（{settings.llm_fallback_provider}）。\n"
                "如需恢复，请发送：恢复主模型",
                msg,
            )
            self.logger.info("管理员 %s (%s) 手动切换到备用模型", sender_nick, sender_id)
        else:
            self.reply_text(
                f"当前已在使用备用模型：{settings.llm_fallback_model}，无需重复切换。", msg
            )


def start_fastapi_server():
    """在后台线程中启动 FastAPI (用于接收 Teambition Webhook)"""
    import uvicorn
    from main import app

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")


def main():
    # 单实例锁：防止多个进程同时运行
    _lock_file = pathlib.Path(__file__).parent / ".bot.lock"
    try:
        _lock_fd = open(_lock_file, "w")
        msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        print(f"[ERROR] 另一个实例正在运行，退出。(lock: {_lock_file})", flush=True)
        sys.exit(1)

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

