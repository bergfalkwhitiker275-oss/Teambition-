"""
手动测试脚本 - 模拟钉钉回调和 Teambition 事件

使用方式:
    1. 先启动服务: cd dingtalk_bot && uvicorn main:app --port 8000 --reload
    2. 运行测试: python test_manual.py
"""

import httpx
import json
import time
import hmac
import hashlib
import base64

BASE_URL = "http://localhost:8000"


def generate_sign(secret: str) -> tuple[str, str]:
    """生成钉钉签名"""
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def test_health():
    """测试健康检查"""
    print("=" * 50)
    print("测试: 健康检查")
    resp = httpx.get(f"{BASE_URL}/health")
    print(f"状态码: {resp.status_code}")
    print(f"响应: {resp.json()}")
    print()


def test_dingtalk_callback(message: str = "让小明下周五之前完成首页设计"):
    """测试钉钉消息回调"""
    print("=" * 50)
    print(f"测试: 钉钉回调 - '{message}'")

    payload = {
        "msgtype": "text",
        "text": {"content": message},
        "msgId": "test_msg_001",
        "createAt": int(time.time() * 1000),
        "conversationType": "2",
        "conversationId": "test_conv_001",
        "senderId": "test_user_001",
        "senderNick": "测试用户",
        "senderCorpId": "test_corp",
        "sessionWebhook": "",
        "sessionWebhookExpiredTime": int(time.time() * 1000) + 3600000,
        "isAdmin": True,
        "atUsers": [],
    }

    resp = httpx.post(
        f"{BASE_URL}/dingtalk/callback",
        json=payload,
        timeout=30.0,
    )
    print(f"状态码: {resp.status_code}")
    print(f"响应: {json.dumps(resp.json(), ensure_ascii=False, indent=2)}")
    print()


def test_teambition_webhook():
    """测试 Teambition 任务状态变更事件"""
    print("=" * 50)
    print("测试: Teambition Webhook - 任务完成事件")

    payload = {
        "event": "task.update",
        "data": {
            "_id": "test_task_001",
            "content": "首页设计",
            "_executorId": "user_001",
            "_creatorId": "user_002",
            "isDone": True,
        },
        "changeData": {
            "fieldName": "isDone",
            "oldValue": False,
            "newValue": True,
        },
        "operator": {
            "_id": "user_001",
            "name": "小明",
        },
    }

    resp = httpx.post(
        f"{BASE_URL}/teambition/webhook",
        json=payload,
        timeout=10.0,
    )
    print(f"状态码: {resp.status_code}")
    print(f"响应: {json.dumps(resp.json(), ensure_ascii=False, indent=2)}")
    print()


if __name__ == "__main__":
    print("钉钉 Teambition 任务机器人 - 手动测试")
    print(f"目标服务: {BASE_URL}")
    print()

    # 1. 健康检查
    test_health()

    # 2. 测试钉钉消息 (会调用 LLM, 需要配置 LLM_API_KEY)
    test_dingtalk_callback("让小明下周五之前完成首页设计")
    test_dingtalk_callback("紧急！张三今天修复登录页面的Bug")
    test_dingtalk_callback("你好")  # 测试无法解析的消息

    # 3. 测试 Teambition 事件
    test_teambition_webhook()

    print("全部测试完成!")
