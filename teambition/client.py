"""Teambition API 客户端 - 通过钉钉开放平台调用

阿里云版 Teambition 的 API 统一通过钉钉开放平台访问:
- Token: GET https://oapi.dingtalk.com/gettoken?appkey=xxx&appsecret=xxx
- API:   https://api.dingtalk.com/v1.0/project/...
- Auth:  Header x-acs-dingtalk-access-token
"""

import json
import time
import logging
import jwt
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

# 钉钉 API 地址
DINGTALK_OAPI_BASE = "https://oapi.dingtalk.com"
DINGTALK_API_BASE = "https://api.dingtalk.com"

# Teambition 开放平台 API 地址
TEAMBITION_API_BASE = "https://open.teambition.com/api"


class TeambitionClient:
    """通过钉钉开放平台调用 Teambition 项目管理 API"""

    def __init__(self):
        self._settings = get_settings()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        # Teambition 开放平台独立 token
        self._tb_access_token: Optional[str] = None
        self._tb_token_expires_at: float = 0
        # 姓名 -> userId 映射缓存
        self._user_map: dict[str, str] = {}
        # 铉钉 userId -> Teambition memberId 映射缓存
        self._tb_member_map: dict[str, str] = {}

    # ============================================================
    # 认证
    # ============================================================

    async def _ensure_token(self) -> str:
        """获取钉钉企业内部应用的 access_token"""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{DINGTALK_OAPI_BASE}/gettoken",
                params={
                    "appkey": self._settings.dingtalk_app_key,
                    "appsecret": self._settings.dingtalk_app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("errcode") != 0:
                raise RuntimeError(f"获取 access_token 失败: {data.get('errmsg')}")

            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 7200)
            self._token_expires_at = time.time() + expires_in - 300  # 提前5分钟刷新

            logger.info("钉钉 access_token 获取成功, 有效期 %ds", expires_in)
            return self._access_token

    async def _ensure_tb_token(self) -> str:
        """Teambition 开放平台 appAccessToken（本地 JWT 签发）"""
        if self._tb_access_token and time.time() < self._tb_token_expires_at:
            return self._tb_access_token

        app_id = self._settings.teambition_app_id
        app_secret = self._settings.teambition_app_secret
        if not app_id or not app_secret:
            raise RuntimeError("未配置 TEAMBITION_APP_ID / TEAMBITION_APP_SECRET")

        now = int(time.time())
        payload = {
            "_appId": app_id,
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(payload, app_secret, algorithm="HS256")
        self._tb_access_token = token
        self._tb_token_expires_at = now + 3300  # 提前5分钟刷新

        logger.info("Teambition appAccessToken 本地签发成功, 有效期 3600s")
        return self._tb_access_token

    async def _tb_request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        operator_id: Optional[str] = None,
    ) -> dict:
        """Teambition 开放平台 API 请求，先尝试 JWT，失败则尝试钉钉 token"""
        tb_token = await self._ensure_tb_token()
        dd_token = await self._ensure_token()

        # 优先尝试 JWT token，失败后回退到钉钉 token
        for token_type, token in [("jwt", tb_token), ("dingtalk", dd_token)]:
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Tenant-Id": self._settings.teambition_org_id,
                "X-Tenant-Type": "organization",
                "Content-Type": "application/json",
            }
            if operator_id:
                headers["x-operator-id"] = operator_id

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method,
                    f"{TEAMBITION_API_BASE}{path}",
                    headers=headers,
                    json=json,
                    params=params,
                )
                data = resp.json() if resp.status_code < 500 else {}
                code = data.get("code", 0) if isinstance(data, dict) else 0

                # 认证失败(403)且还有备选 token，继续尝试
                if (resp.status_code == 403 or code == 403) and token_type == "jwt":
                    logger.warning("JWT token 认证失败，尝试钉钉 token...")
                    continue

                if resp.status_code >= 400:
                    logger.error(
                        "TB API 请求失败: %s %s -> %d, body: %s",
                        method, path, resp.status_code, resp.text
                    )
                    resp.raise_for_status()

                # 检查业务层错误码
                error_code = data.get("errorCode", "") if isinstance(data, dict) else ""
                if error_code and str(error_code) != "":
                    error_msg = data.get("errorMessage", "")
                    logger.warning("TB API 业务错误: %s %s -> errorCode=%s, msg=%s", method, path, error_code, error_msg)

                logger.info("TB API 成功 (%s token): %s %s", token_type, method, path)
                return data

        raise RuntimeError(f"Teambition API 调用失败: {method} {path}")

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """统一的 API 请求方法 (走 api.dingtalk.com)"""
        token = await self._ensure_token()
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method,
                f"{DINGTALK_API_BASE}{path}",
                headers=headers,
                json=json,
                params=params,
            )
            if resp.status_code >= 400:
                # 记录详细错误信息
                logger.error(
                    "API 请求失败: %s %s -> %d, body: %s",
                    method, path, resp.status_code, resp.text
                )
                resp.raise_for_status()
            return resp.json()

    # ============================================================
    # 项目成员相关
    # ============================================================

    async def get_project_members(
        self, operator_id: str, project_id: Optional[str] = None
    ) -> list[dict]:
        """
        获取 Teambition 项目成员列表

        API: GET /v1.0/project/users/{userId}/projects/{projectId}/members
        返回: [{"memberId": "xxx", "userId": "xxx", "role": 0}, ...]
        role: 0=成员, 1=管理员, 2=拥有者
        """
        pid = project_id or self._settings.teambition_default_project_id
        try:
            data = await self._request(
                "GET",
                f"/v1.0/project/users/{operator_id}/projects/{pid}/members",
                params={"maxResults": 300},
            )
            return data.get("result", [])
        except Exception as e:
            logger.error("获取项目成员失败: %s", e)
            return []

    async def get_project_admins(
        self, operator_id: str, project_id: Optional[str] = None
    ) -> list[str]:
        """
        获取项目管理员的钉钉 userId 列表

        role: 1=管理员, 2=拥有者
        """
        members = await self.get_project_members(operator_id, project_id)
        admin_ids = []
        for m in members:
            role = m.get("role", 0)
            uid = m.get("userId", "")
            if uid and role in (1, 2):
                admin_ids.append(uid)
        logger.info("项目管理员: %s (共 %d 人)", admin_ids, len(admin_ids))
        return admin_ids

    async def get_user_detail(self, user_id: str) -> Optional[dict]:
        """
        通过钉钉通讯录 API 获取用户详情 (包含姓名)

        API: POST /topapi/v2/user/get
        """
        try:
            token = await self._ensure_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{DINGTALK_OAPI_BASE}/topapi/v2/user/get",
                    params={"access_token": token},
                    json={"userid": user_id},
                )
                data = resp.json()
                if data.get("errcode") == 0:
                    return data.get("result", {})
                else:
                    logger.warning("获取用户详情失败: %s", data.get("errmsg"))
        except Exception as e:
            logger.error("获取用户详情异常: %s", e)
        return None

    async def _load_project_members(self, operator_id: str) -> None:
        """
        加载项目成员并建立 姓名->userId 映射缓存

        流程: 获取项目成员列表 -> 逐个查询用户姓名 -> 缓存
        """
        members = await self.get_project_members(operator_id)
        logger.info("项目成员数: %d", len(members))
        if members:
            logger.info("成员示例字段: %s", members[0])

        for member in members:
            uid = member.get("userId", "")
            if not uid or uid in [v for v in self._user_map.values()]:
                continue
            detail = await self.get_user_detail(uid)
            if detail:
                name = detail.get("name", "")
                if name:
                    self._user_map[name] = uid
                    logger.info("缓存项目成员: %s -> %s", name, uid)

    def resolve_user_name(self, user_id: str) -> str:
        """根据 userId 反查姓名，未找到返回 userId 本身"""
        for name, uid in self._user_map.items():
            if uid == user_id:
                return name
        return user_id

    @staticmethod
    def format_submit_code(task_id: str, unique_id: int, title: str, executor_name: str) -> str:
        """生成提交代码字符串，用于代码提交信息

        格式: --tbid=MAD-44 --tbtitle=任务标题 --tburl=链接 --user=执行人
        """
        task_url = f"https://www.teambition.com/task/{task_id}" if task_id else ""
        parts = []
        if unique_id:
            parts.append(f"--tbid=MAD-{unique_id}")
        if title:
            parts.append(f"--tbtitle={title}")
        if task_url:
            parts.append(f"--tburl={task_url}")
        if executor_name:
            parts.append(f"--user={executor_name}")
        return " ".join(parts) if parts else ""

    async def ensure_user_names(self, user_ids: list[str]) -> None:
        """确保给定 userId 列表的姓名都在缓存中，未缓存的则查询"""
        known_ids = set(self._user_map.values())
        unknown_ids = [uid for uid in set(user_ids) if uid and uid not in known_ids]
        for uid in unknown_ids:
            detail = await self.get_user_detail(uid)
            if detail:
                name = detail.get("name", "")
                if name:
                    self._user_map[name] = uid
                    logger.info("补充缓存用户: %s -> %s", name, uid)

    async def resolve_tb_member_id(self, dd_user_id: str) -> Optional[str]:
        """根据铉钉 userId 获取 Teambition userId，用于 x-operator-id"""
        if dd_user_id in self._tb_member_map:
            return self._tb_member_map[dd_user_id]
        # 通过铉钉 API 获取 TB userId
        # GET /v1.0/project/teambition/users?optUserId=xxx&userId=xxx
        try:
            data = await self._request(
                "GET",
                "/v1.0/project/teambition/users",
                params={"optUserId": dd_user_id, "userId": dd_user_id},
            )
            tb_uid = ""
            result = data.get("result", {})
            if isinstance(result, dict):
                tb_uid = result.get("tbUserId", "")
            if tb_uid:
                self._tb_member_map[dd_user_id] = tb_uid
                logger.info("铉钉->TB userId 映射: %s -> %s", dd_user_id, tb_uid)
                return tb_uid
            else:
                logger.warning("铉钉 API 返回无 tbUserId: %s", data)
        except Exception as e:
            logger.error("获取 TB userId 失败: %s", e)
        return None

    async def resolve_user_id(self, name: str, operator_id: Optional[str] = None) -> Optional[str]:
        """
        根据用户姓名查找钉钉 userId

        策略: 拉取项目成员列表, 查询每个成员姓名, 按姓名匹配
        """
        # 先查缓存
        if name in self._user_map:
            return self._user_map[name]

        # 加载项目成员
        if not self._user_map and operator_id:
            await self._load_project_members(operator_id)

        # 精确匹配
        if name in self._user_map:
            return self._user_map[name]

        # 模糊匹配
        for cached_name, cached_id in self._user_map.items():
            if name in cached_name or cached_name in name:
                logger.info("模糊匹配用户: '%s' -> '%s' (%s)", name, cached_name, cached_id)
                return cached_id

        logger.warning("未找到项目成员 '%s'", name)
        return None

    # ============================================================
    # 任务相关
    # ============================================================

    async def create_task(
        self,
        title: str,
        operator_id: str,
        assignee_id: Optional[str] = None,
        due_date: Optional[str] = None,
        priority: Optional[int] = None,
        project_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """
        创建 Teambition 项目任务

        Args:
            title: 任务标题
            operator_id: 操作者的钉钉 userId (必须)
            assignee_id: 负责人的钉钉 userId
            due_date: 截止日期, ISO 8601 格式
            priority: 优先级 (0=普通, 1=紧急, 2=非常紧急)
            project_id: 项目 ID
            note: 备注
        """
        pid = project_id or self._settings.teambition_default_project_id

        payload: dict = {
            "projectId": pid,
            "content": title,
        }

        if assignee_id:
            payload["executorId"] = assignee_id
        if due_date:
            payload["dueDate"] = due_date
        if priority is not None:
            payload["priority"] = priority
        if note:
            payload["note"] = note

        data = await self._request(
            "POST",
            f"/v1.0/project/users/{operator_id}/tasks",
            json=payload,
        )

        result = data.get("result", data)
        task_id = result.get("taskId", "")
        logger.info("任务创建成功: taskId=%s, title=%s, create_keys=%s", task_id, title, list(result.keys()))

        # 创建接口不返回 uniqueId，通过铉钉 API 补查任务详情
        if task_id and not result.get("uniqueId"):
            try:
                detail = await self.get_task_detail_tb(task_id, operator_id)
                if detail:
                    uid = detail.get("uniqueId")
                    if uid:
                        result["uniqueId"] = uid
                        logger.info("补查 uniqueId=%s (MAD-%s)", uid, uid)
                    else:
                        logger.warning("任务详情中未找到 uniqueId, keys=%s", list(detail.keys()))
                else:
                    logger.warning("补查任务详情返回 None")
            except Exception as e:
                logger.warning("补查 uniqueId 失败: %s", e)

        return result

    async def get_task(self, task_id: str, operator_id: str) -> dict:
        """获取任务详情"""
        data = await self._request(
            "GET",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}",
        )
        return data.get("result", data)

    async def query_project_tasks(
        self,
        operator_id: str,
        project_id: Optional[str] = None,
        max_results: int = 200,
    ) -> list[dict]:
        """查询项目中的任务"""
        pid = project_id or self._settings.teambition_default_project_id
        data = await self._request(
            "GET",
            f"/v1.0/project/users/{operator_id}/projectIds/{pid}/tasks",
            params={"maxResults": max_results},
        )
        return data.get("result", [])

    async def search_task_by_title(
        self,
        title: str,
        operator_id: str,
        project_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        通过标题搜索项目中的任务

        先拉取项目任务列表，然后按标题模糊匹配
        返回匹配度最高的任务，或 None
        """
        tasks = await self.query_project_tasks(operator_id, project_id)
        logger.info("搜索任务 '%s'，项目中共 %d 个任务", title, len(tasks))

        # 精确匹配
        for task in tasks:
            content = task.get("content", "")
            if content == title:
                logger.info("精确匹配到任务: %s (id=%s)", content, task.get("taskId"))
                return task

        # 模糊匹配: 任务名包含搜索词 或 搜索词包含任务名
        best_match = None
        best_score = 0
        for task in tasks:
            content = task.get("content", "")
            if not content:
                continue
            # 双向包含检查
            if title in content or content in title:
                score = len(title) / max(len(content), 1)
                if score > best_score:
                    best_score = score
                    best_match = task

        if best_match:
            logger.info(
                "模糊匹配到任务: '%s' (id=%s)",
                best_match.get("content"), best_match.get("taskId")
            )
        else:
            logger.warning("未找到匹配任务: '%s'", title)

        return best_match

    # ============================================================
    # 更新任务
    # ============================================================

    async def update_task_priority(
        self, task_id: str, operator_id: str, priority: int
    ) -> dict:
        """
        更新任务优先级

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/priorities
        priority: -10(较低), 0(普通), 1(紧急), 2(非常紧急)
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/priorities",
            json={"priority": priority},
        )
        logger.info("任务 %s 优先级已更新为 %d", task_id, priority)
        return data.get("result", data)

    async def update_task_executor(
        self, task_id: str, operator_id: str, executor_id: str
    ) -> dict:
        """
        更新任务执行者

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/executors
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/executors",
            json={"executorId": executor_id},
        )
        logger.info("任务 %s 执行者已更新为 %s", task_id, executor_id)
        return data.get("result", data)

    async def update_task_due_date(
        self, task_id: str, operator_id: str, due_date: str
    ) -> dict:
        """
        更新任务截止时间

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/dueDates
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/dueDates",
            json={"dueDate": due_date},
        )
        logger.info("任务 %s 截止时间已更新为 %s", task_id, due_date)
        return data.get("result", data)

    async def update_task_content(
        self, task_id: str, operator_id: str, content: str
    ) -> dict:
        """
        更新任务标题

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/contents
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/contents",
            json={"content": content},
        )
        logger.info("任务 %s 标题已更新为 '%s'", task_id, content)
        return data.get("result", data)

    async def update_task_note(
        self, task_id: str, operator_id: str, note: str
    ) -> dict:
        """
        更新任务备注

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/notes
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/notes",
            json={"note": note},
        )
        logger.info("任务 %s 备注已更新", task_id)
        return data.get("result", data)

    async def update_task_start_date(
        self, task_id: str, operator_id: str, start_date: str
    ) -> dict:
        """
        更新任务开始时间

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/startDates
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/startDates",
            json={"startDate": start_date},
        )
        logger.info("任务 %s 开始时间已更新为 %s", task_id, start_date)
        return data.get("result", data)

    async def update_task_participants(
        self, task_id: str, operator_id: str,
        add_ids: Optional[list[str]] = None,
        del_ids: Optional[list[str]] = None,
    ) -> dict:
        """
        更新任务参与者

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/involveMembers
        """
        payload = {}
        if add_ids:
            payload["addInvolvers"] = add_ids
        if del_ids:
            payload["delInvolvers"] = del_ids
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/involveMembers",
            json=payload,
        )
        logger.info("任务 %s 参与者已更新", task_id)
        return data.get("result", data)

    # ============================================================
    # 任务工作流/状态
    # ============================================================

    # ============================================================
    # 迭代 (Sprint)
    # ============================================================

    async def get_project_sprints(
        self, operator_id: str, project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        查询项目中的迭代列表

        API: POST https://open.teambition.com/api/sprint/query
        """
        pid = project_id or self._settings.teambition_default_project_id
        data = await self._tb_request(
            "POST",
            "/sprint/query",
            json={"projectId": pid, "pageSize": 100},
        )
        logger.info("迭代查询响应: %s", data)
        result = data.get("result") or []
        logger.info("项目迭代数: %d", len(result))
        return result

    async def resolve_sprint_id(
        self, sprint_name: str, operator_id: str, project_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        根据迭代名称查找迭代 ID
        """
        sprints = await self.get_project_sprints(operator_id, project_id)
        for s in sprints:
            name = s.get("name", "")
            if name == sprint_name or sprint_name in name:
                logger.info("匹配迭代: '%s' -> %s", sprint_name, s.get("sprintId"))
                return s.get("sprintId", "")
        logger.warning("未找到迭代: '%s'", sprint_name)
        return None

    async def update_task_sprint(
        self, task_id: str, operator_id: str, sprint_id: str,
    ) -> dict:
        """
        更新任务的迭代

        API: PUT https://open.teambition.com/api/v3/task/{taskId}/sprint
        """
        # 解析 Teambition userId 作为 x-operator-id
        tb_user_id = await self.resolve_tb_member_id(operator_id)
        if not tb_user_id:
            logger.error("无法解析 TB userId, dd_user_id=%s", operator_id)
        op_id = tb_user_id or operator_id
        logger.info("迭代更新 operator: dd=%s -> tb=%s", operator_id, op_id)
        data = await self._tb_request(
            "PUT",
            f"/v3/task/{task_id}/sprint",
            json={"sprintId": sprint_id},
            operator_id=op_id,
        )
        logger.info("任务 %s 迭代更新响应: %s", task_id, data)
        # 检查业务层错误
        error_code = data.get("errorCode", "") if isinstance(data, dict) else ""
        if error_code:
            error_msg = data.get("errorMessage", "")
            raise RuntimeError(f"迭代更新失败: {error_msg} (errorCode={error_code})")
        return data.get("result", data)

    async def get_task_detail_tb(self, task_id: str, operator_id: Optional[str] = None) -> Optional[dict]:
        """通过铉钉 API 获取任务详情（含 sprintId）

        API: GET /v1.0/project/users/{userId}/tasks?taskId={taskId}
        """
        try:
            data = await self._request(
                "GET",
                f"/v1.0/project/users/{operator_id}/tasks",
                params={"taskId": task_id},
            )
            result = data.get("result", data)
            # API 返回列表，取第一个元素
            if isinstance(result, list) and len(result) > 0:
                result = result[0]
            logger.info("任务详情 %s: sprintId=%s", task_id, result.get("sprintId", "NOT_FOUND") if isinstance(result, dict) else "not_dict")
            return result if isinstance(result, dict) else None
        except Exception as e:
            logger.error("获取任务详情失败 %s: %s", task_id, e)
            return None

    async def query_tasks_by_sprint(
        self, sprint_id: str, operator_id: str, project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        查询迭代下的任务列表

        方案: 获取所有任务 -> 逐个查详情获取 sprintId -> 过滤匹配的任务
        """
        pid = project_id or self._settings.teambition_default_project_id
        # 获取项目所有任务
        all_tasks = await self.query_project_tasks(operator_id, pid)
        if not all_tasks:
            logger.info("项目无任务，迭代查询返回空")
            return []

        matched = []
        for task in all_tasks:
            tid = task.get("taskId", "")
            if not tid:
                continue
            # 使用铉钉 userId 获取任务详情
            detail = await self.get_task_detail_tb(tid, operator_id=operator_id)
            if not detail:
                continue
            task_sprint = detail.get("sprintId", "")
            if task_sprint == sprint_id:
                # 合并铉钉任务基础字段 + TB详情字段
                merged = {**task, **detail}
                if "_id" in merged and "taskId" not in merged:
                    merged["taskId"] = merged["_id"]
                matched.append(merged)
        logger.info("迭代 %s 下共 %d 个任务 (总任务 %d)", sprint_id, len(matched), len(all_tasks))
        return matched

    async def get_task_workflow_statuses(
        self, task_id: str, operator_id: str,
        project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        获取项目的工作流状态列表

        API: GET /v1.0/project/users/{userId}/projects/{projectId}/taskflowStatuses/search
        返回: [{"id": "xxx", "name": "未开始", "taskflowId": "xxx"}, ...]
        """
        pid = project_id or self._settings.teambition_default_project_id
        data = await self._request(
            "GET",
            f"/v1.0/project/users/{operator_id}/projects/{pid}/taskflowStatuses/search",
            params={"maxResults": 300},
        )
        result = data.get("result", [])
        return result

    async def update_task_workflow_status(
        self, task_id: str, operator_id: str, taskflow_status_id: str
    ) -> dict:
        """
        更新任务工作流状态

        API: PUT /v1.0/project/users/{userId}/tasks/{taskId}/taskflowStatuses
        """
        data = await self._request(
            "PUT",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}/taskflowStatuses",
            json={"taskflowStatusId": taskflow_status_id},
        )
        logger.info("任务 %s 工作流状态已更新为 %s", task_id, taskflow_status_id)
        return data.get("result", data)

    async def complete_task(
        self, task_id: str, operator_id: str
    ) -> dict:
        """
        完成任务: 找到名称包含"完成"/"关闭"的工作流状态并设置
        """
        statuses = await self.get_task_workflow_statuses(task_id, operator_id)
        end_status = None
        for s in statuses:
            name = s.get("name", "")
            if "完成" in name or "已完成" in name or "已关闭" in name or "关闭" in name:
                end_status = s
                break
        if not end_status:
            raise RuntimeError(f"任务 {task_id} 找不到完成状态，可用状态: {[s.get('name') for s in statuses]}")

        result = await self.update_task_workflow_status(
            task_id, operator_id, end_status["taskflowStatusId"]
        )
        logger.info("任务 %s 已标记为完成 (status=%s)", task_id, end_status.get("name"))
        return result

    async def reopen_task(
        self, task_id: str, operator_id: str
    ) -> dict:
        """
        重新打开任务: 找到名称包含"未开始"/"待处理"的工作流状态并设置
        """
        statuses = await self.get_task_workflow_statuses(task_id, operator_id)
        start_status = None
        for s in statuses:
            name = s.get("name", "")
            if "未开始" in name or "待处理" in name:
                start_status = s
                break
        if not start_status and statuses:
            start_status = statuses[0]
        if not start_status:
            raise RuntimeError(f"任务 {task_id} 找不到初始状态")

        result = await self.update_task_workflow_status(
            task_id, operator_id, start_status["taskflowStatusId"]
        )
        logger.info("任务 %s 已重新打开 (status=%s)", task_id, start_status.get("name"))
        return result

    async def set_task_status_by_name(
        self, task_id: str, operator_id: str, status_name: str
    ) -> dict:
        """
        按名称设置任务工作流状态 (模糊匹配)
        """
        statuses = await self.get_task_workflow_statuses(task_id, operator_id)
        target = None
        for s in statuses:
            name = s.get("name", "")
            if name == status_name or status_name in name or name in status_name:
                target = s
                break
        if not target:
            available = [s.get("name") for s in statuses]
            raise RuntimeError(f"未找到状态 '{status_name}'，可用状态: {available}")

        result = await self.update_task_workflow_status(
            task_id, operator_id, target["taskflowStatusId"]
        )
        logger.info("任务 %s 状态已设置为 '%s'", task_id, target.get("name"))
        return result

    # ============================================================
    # 删除任务
    # ============================================================

    async def delete_task(
        self, task_id: str, operator_id: str
    ) -> dict:
        """
        删除任务 (移入回收站)

        API: DELETE /v1.0/project/users/{userId}/tasks/{taskId}
        """
        data = await self._request(
            "DELETE",
            f"/v1.0/project/users/{operator_id}/tasks/{task_id}",
        )
        logger.info("任务 %s 已删除", task_id)
        return data.get("result", data)

    # ============================================================
    # 查询任务
    # ============================================================

    async def query_user_tasks(
        self, operator_id: str, target_user_id: Optional[str] = None,
        max_results: int = 50,
    ) -> list[dict]:
        """查询某人的任务列表"""
        tasks = await self.query_project_tasks(operator_id)
        uid = target_user_id or operator_id
        user_tasks = [
            t for t in tasks
            if t.get("executorId") == uid
        ]
        logger.info("用户 %s 共有 %d 个任务", uid, len(user_tasks))
        return user_tasks[:max_results]

    async def query_tasks_by_status(
        self, operator_id: str, status_filter: str,
        project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        按状态筛选任务

        status_filter:
          - "undone": 未完成的任务
          - "done": 已完成的任务
          - "overdue": 逾期任务 (截止日期已过且未完成)
          - 其他: 按工作流状态名称筛选 (如 "未开始"/"进行中"/"已完成")
        """
        tasks = await self.query_project_tasks(operator_id, project_id)
        now = datetime.now(timezone.utc)

        if status_filter == "undone":
            filtered = [t for t in tasks if not t.get("isDone", False)]
        elif status_filter == "done":
            filtered = [t for t in tasks if t.get("isDone", False)]
        elif status_filter == "overdue":
            filtered = []
            for t in tasks:
                if t.get("isDone", False):
                    continue
                due = t.get("dueDate", "")
                if due:
                    try:
                        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        if due_dt < now:
                            filtered.append(t)
                    except (ValueError, TypeError):
                        pass
        else:
            # 按工作流状态名称筛选
            # API 返回的任务只有 taskflowstatusId，需要先获取状态名称映射
            status_id_to_name = {}
            if tasks:
                try:
                    statuses = await self.get_task_workflow_statuses(
                        tasks[0]["taskId"], operator_id
                    )
                    for s in statuses:
                        sid = s.get("taskflowStatusId", "")
                        sname = s.get("name", "")
                        if sid:
                            status_id_to_name[sid] = sname
                    logger.info("工作流状态映射: %s", status_id_to_name)
                except Exception as e:
                    logger.warning("获取工作流状态失败: %s", e)

            # 给每个任务注入 taskflowStatusName
            for t in tasks:
                sid = t.get("taskflowstatusId", "")
                t["taskflowStatusName"] = status_id_to_name.get(sid, "")

            filtered = [
                t for t in tasks
                if status_filter in (t.get("taskflowStatusName", "") or "")
            ]

        logger.info("状态筛选 '%s': 共 %d 个任务", status_filter, len(filtered))
        return filtered


# 全局单例
_client: Optional[TeambitionClient] = None


def get_teambition_client() -> TeambitionClient:
    """获取 Teambition 客户端单例"""
    global _client
    if _client is None:
        _client = TeambitionClient()
    return _client
