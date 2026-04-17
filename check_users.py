"""查询企业用户列表"""
import httpx

token_resp = httpx.get(
    "https://oapi.dingtalk.com/gettoken",
    params={
        "appkey": "dingu0gp0jux77l3caza",
        "appsecret": "3ByJ4eXN2nXuUgJ_Npxwf8WdrfoXoZkB_k-fXCh1oW7_RSL8Fo-rAIIP5zvmOe92",
    },
).json()

token = token_resp["access_token"]
print(f"Token: {token[:20]}...")

users_resp = httpx.post(
    "https://oapi.dingtalk.com/topapi/v2/user/list",
    params={"access_token": token},
    json={"dept_id": 1, "cursor": 0, "size": 50},
).json()

print(f"errcode: {users_resp.get('errcode')}")
users = users_resp.get("result", {}).get("list", [])
print(f"\n企业用户列表 (共 {len(users)} 人):")
for u in users:
    print(f"  {u.get('name')} -> userId: {u.get('userid')}")
