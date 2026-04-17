"""钉钉消息回调处理 - 签名验证与消息解析"""

import hashlib
import hmac
import base64
import time
import logging
from typing import Optional

from fastapi import Request, HTTPException

from config import get_settings

logger = logging.getLogger(__name__)


def verify_signature(timestamp: str, sign: str) -> bool:
    """
    验证钉钉回调请求的签名
    签名算法: HmacSHA256(timestamp + "\n" + app_secret)
    """
    settings = get_settings()
    app_secret = settings.dingtalk_app_secret

    if not app_secret:
        logger.warning("DINGTALK_APP_SECRET 未配置, 跳过签名验证")
        return True

    # 检查时间戳是否在合理范围内 (1小时内)
    current_time = int(time.time() * 1000)
    if abs(current_time - int(timestamp)) > 3600000:
        logger.warning("时间戳超出合理范围: %s", timestamp)
        return False

    string_to_sign = f"{timestamp}\n{app_secret}"
    hmac_code = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    expected_sign = base64.b64encode(hmac_code).decode("utf-8")

    return sign == expected_sign


async def parse_dingtalk_message(request: Request) -> dict:
    """
    解析钉钉机器人收到的消息

    钉钉消息体结构:
    {
        "msgtype": "text",
        "text": {"content": "创建任务 ..."},
        "msgId": "xxx",
        "createAt": 1234567890,
        "conversationType": "1"(单聊) / "2"(群聊),
        "conversationId": "xxx",
        "senderId": "xxx",
        "senderNick": "张三",
        "senderCorpId": "xxx",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=xxx",
        "sessionWebhookExpiredTime": 1234567890,
        "isAdmin": true/false,
        "chatbotUserId": "xxx",
        "isInAtList": true/false,
        "atUsers": [{"dingtalkId": "xxx", "staffId": "xxx"}]
    }
    """
    # 获取签名信息
    timestamp = request.headers.get("timestamp", "")
    sign = request.headers.get("sign", "")

    if timestamp and sign:
        if not verify_signature(timestamp, sign):
            raise HTTPException(status_code=403, detail="签名验证失败")

    body = await request.json()
    logger.info("收到钉钉消息: msgId=%s, sender=%s", body.get("msgId"), body.get("senderNick"))

    # 提取消息文本 (去掉 @机器人 的部分)
    text_content = ""
    if body.get("msgtype") == "text":
        text_content = body.get("text", {}).get("content", "").strip()

    return {
        "msg_id": body.get("msgId", ""),
        "msg_type": body.get("msgtype", "text"),
        "text": text_content,
        "conversation_type": body.get("conversationType", "1"),
        "conversation_id": body.get("conversationId", ""),
        "sender_id": body.get("senderId", ""),
        "sender_nick": body.get("senderNick", ""),
        "sender_corp_id": body.get("senderCorpId", ""),
        "session_webhook": body.get("sessionWebhook", ""),
        "session_webhook_expired_time": body.get("sessionWebhookExpiredTime", 0),
        "is_admin": body.get("isAdmin", False),
        "at_users": body.get("atUsers", []),
        "raw": body,
    }
