"""用真实 userId 测试创建任务"""
import httpx
import json
import time

BASE_URL = "http://localhost:8000"

payload = {
    "msgtype": "text",
    "text": {"content": "给吕鑫下周五之前完成首页设计"},
    "msgId": "test_real_002",
    "createAt": int(time.time() * 1000),
    "conversationType": "2",
    "conversationId": "c1",
    "senderId": "124655202621300974",  # 单慧楠 的真实 userId (项目成员)
    "senderNick": "单慧楠",
    "senderCorpId": "corp1",
    "sessionWebhook": "",
    "sessionWebhookExpiredTime": 0,
    "isAdmin": True,
    "atUsers": [],
}

print("发送测试消息: 给吕鑫下周五之前完成首页设计")
print(f"操作者: 单慧楠 (userId: 124655202621300974)")
print()

r = httpx.post(f"{BASE_URL}/dingtalk/callback", json=payload, timeout=30)
print(f"状态码: {r.status_code}")
print(f"响应: {json.dumps(r.json(), ensure_ascii=False, indent=2)}")
