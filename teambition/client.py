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
        # TB 原生用户 userId 集合（这些用户没有钉钉账号，创建任务时需特殊处理）
        self._tb_native_users: set[str] = set()
        # 项目前缀缓存 (如 "BP3")
        self._project_key: Optional[str] = None

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
        last_error = None
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

                # 认证失败(401/403)且还有备选 token，继续尝试
                if (resp.status_code in (401, 403) or code in (401, 403)) and token_type == "jwt":
                    logger.warning(
                        "JWT token 认证失败(%s %s), http=%d, code=%d, body=%s，尝试钉钉 token...",
                        method, path, resp.status_code, code, resp.text[:500],
                    )
                    # 清除 JWT 缓存以便下次重新签发
                    self._tb_access_token = None
                    self._tb_token_expires_at = 0
                    last_error = resp
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
    # 项目信息
    # ============================================================

    async def get_project_key(self, operator_id: str = None, project_id: Optional[str] = None) -> str:
        """获取项目前缀 (如 'BP3')，从配置文件读取"""
        if self._project_key:
            return self._project_key
        key = self._settings.teambition_project_key
        if key:
            self._project_key = key
            logger.info("项目前缀: %s", key)
        else:
            logger.warning("未配置 TEAMBITION_PROJECT_KEY，--tbid 将使用原始 taskId")
        return key

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

    async def _search_dd_user_by_name(self, name: str, mobile: str = "") -> Optional[str]:
        """通过姓名或手机号在钉钉通讯录中查找用户的 userId

        策略: 1) 先用手机号查找  2) 再遍历部门树按姓名匹配
        """
        try:
            token = await self._ensure_token()

            # 策略 1: 通过手机号查找
            if mobile:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{DINGTALK_OAPI_BASE}/topapi/v2/user/getbymobile",
                        params={"access_token": token},
                        json={"mobile": mobile},
                    )
                    data = resp.json()
                    if data.get("errcode") == 0:
                        dd_uid = data.get("result", {}).get("userid", "")
                        if dd_uid:
                            logger.info("钉钉通讯录按手机号找到: %s (%s) -> %s", name, mobile, dd_uid)
                            return dd_uid
                    else:
                        logger.info("手机号查找失败(%s): %s", mobile, data.get("errmsg"))

            # 策略 2: 遍历部门树按姓名匹配
            # 递归获取所有部门 ID
            dept_ids = [1]  # 从根部门开始
            queue = [1]
            async with httpx.AsyncClient(timeout=10.0) as client:
                while queue:
                    parent_id = queue.pop(0)
                    resp = await client.post(
                        f"{DINGTALK_OAPI_BASE}/topapi/v2/department/listsub",
                        params={"access_token": token},
                        json={"dept_id": parent_id},
                    )
                    data = resp.json()
                    if data.get("errcode") == 0:
                        for d in data.get("result", []):
                            did = d.get("dept_id")
                            if did:
                                dept_ids.append(did)
                                queue.append(did)

            logger.info("钉钉部门树: 共 %d 个部门", len(dept_ids))

            # 遍历每个部门查找用户
            all_names = []
            for dept_id in dept_ids:
                cursor = 0
                while True:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{DINGTALK_OAPI_BASE}/topapi/v2/user/list",
                            params={"access_token": token},
                            json={"dept_id": dept_id, "cursor": cursor, "size": 100},
                        )
                        data = resp.json()
                        if data.get("errcode") != 0:
                            logger.warning("列出部门 %s 用户失败: %s", dept_id, data.get("errmsg"))
                            break
                        result = data.get("result", {})
                        user_list = result.get("list", [])
                        for user in user_list:
                            uname = user.get("name", "")
                            all_names.append(uname)
                            if uname == name:
                                dd_uid = user.get("userid", "")
                                logger.info("钉钉通讯录按姓名找到: %s -> %s", name, dd_uid)
                                return dd_uid
                        if not result.get("has_more"):
                            break
                        cursor = result.get("next_cursor", 0)
            logger.warning("钉钉通讯录未找到 '%s'，全部 %d 人: %s", name, len(all_names), all_names[:20])
        except Exception as e:
            logger.warning("钉钉通讯录按姓名搜索失败: %s", e)
        return None

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

        流程:
        1. 先通过钉钉通讯录获取成员姓名（优先用钉钉 userId）
        2. 对于钉钉通讯录查不到的用户（TB 原生用户），通过 TB 企业成员 API 补充
        """
        # Step 1: 通过钉钉 API 获取项目成员列表
        members = await self.get_project_members(operator_id)
        logger.info("钉钉项目成员数: %d", len(members))
        if members:
            logger.info("成员示例字段: %s", members[0])

        failed_uids = []
        for member in members:
            uid = member.get("userId", "")
            if not uid or uid in self._user_map.values():
                continue
            detail = await self.get_user_detail(uid)
            if detail:
                name = detail.get("name", "")
                if name and name not in self._user_map:
                    self._user_map[name] = uid
                    logger.info("缓存项目成员: %s -> %s", name, uid)
                elif not name:
                    failed_uids.append(uid)
            else:
                failed_uids.append(uid)

        # Step 2: 对于钉钉通讯录查不到的用户，通过 TB 企业成员 API 补充
        if failed_uids:
            logger.warning("以下 %d 个成员无法通过钉钉通讯录查询姓名: %s", len(failed_uids), failed_uids)
            await self._load_members_from_tb(operator_id, failed_uids)

        logger.info("成员缓存加载完成，共 %d 人: %s", len(self._user_map), list(self._user_map.keys()))

    async def _load_members_from_tb(self, operator_id: str, target_uids: list[str]) -> None:
        """通过 Teambition 企业成员 API 获取带姓名的成员列表

        从企业成员列表中查找 target_uids 对应的姓名。
        这些用户是 TB 原生用户，其 userId 在钉钉通讯录中查不到。
        使用 GET /org/member/list 获取企业成员（含姓名），
        需要 tbs-app:appmember:list 权限（已开通）。
        """
        org_id = self._settings.teambition_org_id
        if not org_id:
            logger.warning("未配置 TEAMBITION_ORG_ID，跳过 TB 企业成员加载")
            return

        target_set = set(target_uids)
        found_count = 0

        try:
            # 分页获取企业成员
            page_token = ""
            while target_set:
                data = await self._tb_request(
                    "GET",
                    "/org/member/list",
                    params={
                        "orgId": org_id,
                        "pageToken": page_token,
                        "pageSize": 200,
                        "filter": "enabled",
                    },
                )
                result = data.get("result", []) if isinstance(data, dict) else []
                if isinstance(result, dict):
                    result = result.get("members", []) or result.get("result", []) or []
                if not isinstance(result, list):
                    result = []

                logger.info("TB 企业成员 API 返回 %d 条记录", len(result))
                if result:
                    logger.info("TB 企业成员示例: %s", json.dumps(result[0], ensure_ascii=False, default=str)[:500])

                for m in result:
                    tb_uid = m.get("userId") or m.get("memberId") or ""
                    if tb_uid not in target_set:
                        continue
                    name = m.get("name") or m.get("nickName") or m.get("nick") or ""
                    if name:
                        # 获取手机号和邮箱，用于在钉钉通讯录中查找
                        mobile = m.get("phone", "") or ""
                        logger.info("TB用户信息: %s, phone=%s, email=%s", name, mobile, m.get("email", ""))
                        # 尝试通过姓名/手机号在钉钉通讯录中找到对应的钉钉 userId
                        dd_uid = await self._search_dd_user_by_name(name, mobile=mobile)
                        if dd_uid:
                            self._user_map[name] = dd_uid
                            logger.info("TB原生用户已映射到钉钉: %s -> DD:%s (TB:%s)", name, dd_uid, tb_uid)
                        else:
                            # 找不到钉钉 userId，使用 TB userId 并标记为 TB 原生用户
                            self._user_map[name] = tb_uid
                            self._tb_native_users.add(tb_uid)
                            logger.warning("TB原生用户未找到钉钉账号: %s -> TB:%s", name, tb_uid)
                        target_set.discard(tb_uid)
                        found_count += 1

                # 检查分页
                next_token = data.get("nextPageToken", "") if isinstance(data, dict) else ""
                if not next_token:
                    break
                page_token = next_token

            if target_set:
                logger.warning("仍有 %d 个成员未找到姓名: %s", len(target_set), target_set)
            logger.info("TB 企业成员 API 补充了 %d 个成员", found_count)
        except Exception as e:
            logger.warning("TB 企业成员 API 查询失败: %s", e)

    def resolve_user_name(self, user_id: str) -> str:
        """根据 userId 反查姓名，未找到返回 userId 本身"""
        for name, uid in self._user_map.items():
            if uid == user_id:
                return name
        return user_id

    def format_submit_code(self, task_id: str, unique_id: int, title: str, executor_name: str) -> str:
        """生成提交代码字符串，用于代码提交信息

        格式: --tbid=BP3-51 --tbtitle=任务标题 --tburl=链接 --user=执行人
        """
        task_url = f"https://www.teambition.com/task/{task_id}" if task_id else ""
        parts = []
        if unique_id and self._project_key:
            parts.append(f"--tbid={self._project_key}-{unique_id}")
        elif task_id:
            parts.append(f"--tbid={task_id}")
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

        # 模糊匹配缓存
        for cached_name, cached_id in self._user_map.items():
            if name in cached_name or cached_name in name:
                logger.info("模糊匹配用户: '%s' -> '%s' (%s)", name, cached_name, cached_id)
                return cached_id

        # 缓存未命中，尝试重新加载项目成员
        if operator_id:
            logger.info("缓存未找到 '%s'，重新加载项目成员...", name)
            await self._load_project_members(operator_id)

        # 精确匹配
        if name in self._user_map:
            return self._user_map[name]

        # 模糊匹配
        for cached_name, cached_id in self._user_map.items():
            if name in cached_name or cached_name in name:
                logger.info("模糊匹配用户: '%s' -> '%s' (%s)", name, cached_name, cached_id)
                return cached_id

        logger.warning("未找到项目成员 '%s'，已缓存成员: %s", name, list(self._user_map.keys()))
        return None

    # ============================================================
    # 任务类型 (场景字段配置)
    # ============================================================

    async def get_scenario_field_configs(
        self, operator_id: str, project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        获取项目的任务类型列表 (场景字段配置)

        API: GET https://open.teambition.com/api/v3/project/{projectId}/scenariofieldconfig/search
        返回: [{"id": "xxx", "name": "需求", ...}, ...]
        """
        pid = project_id or self._settings.teambition_default_project_id
        # 解析 Teambition userId 作为 x-operator-id
        tb_user_id = await self.resolve_tb_member_id(operator_id)
        op_id = tb_user_id or operator_id
        try:
            data = await self._tb_request(
                "GET",
                f"/v3/project/{pid}/scenariofieldconfig/search",
                operator_id=op_id,
            )
            result = data.get("result", [])
            if isinstance(result, dict):
                result = result.get("scenariofieldconfigs", []) or result.get("result", [])
            logger.info("项目任务类型: %s", [(r.get('name'), r.get('id') or r.get('scenariofieldconfigId') or r.get('_id')) for r in result])
            return result
        except Exception as e:
            logger.error("获取任务类型列表失败: %s", e)
            return []

    async def resolve_scenario_field_config_id(
        self, type_name: str, operator_id: str, project_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        根据任务类型名称查找 scenariofieldconfigId

        Args:
            type_name: 任务类型名称, 如 "需求"、"任务"、"缺陷"、"美术"
        """
        configs = await self.get_scenario_field_configs(operator_id, project_id)
        # 精确匹配
        for c in configs:
            name = c.get("name", "")
            config_id = c.get("id") or c.get("scenariofieldconfigId") or c.get("_id", "")
            if name == type_name:
                logger.info("匹配任务类型: '%s' -> %s", type_name, config_id)
                return config_id
        # 模糊匹配
        for c in configs:
            name = c.get("name", "")
            config_id = c.get("id") or c.get("scenariofieldconfigId") or c.get("_id", "")
            if type_name in name or name in type_name:
                logger.info("模糊匹配任务类型: '%s' -> '%s' (%s)", type_name, name, config_id)
                return config_id
        available = [c.get('name') for c in configs]
        logger.warning("未找到任务类型 '%s'，可用类型: %s", type_name, available)
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
        scenario_field_config_id: Optional[str] = None,
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
            scenario_field_config_id: 任务类型 ID (场景字段配置 ID)
        """
        pid = project_id or self._settings.teambition_default_project_id

        payload: dict = {
            "projectId": pid,
            "content": title,
        }

        if assignee_id and assignee_id not in self._tb_native_users:
            payload["executorId"] = assignee_id
        # TB 原生用户不能通过钉钉 API 设置 executorId，需创建后通过 TB API 单独设置
        tb_native_assignee = assignee_id if (assignee_id and assignee_id in self._tb_native_users) else None
        if due_date:
            payload["dueDate"] = due_date
        if priority is not None:
            payload["priority"] = priority
        if note:
            payload["note"] = note
        if scenario_field_config_id:
            payload["scenariofieldconfigId"] = scenario_field_config_id

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

        # 对于 TB 原生用户，创建后通过 TB 开放平台 API 设置执行者
        if task_id and tb_native_assignee:
            try:
                tb_op_id = await self.resolve_tb_member_id(operator_id)
                op_id = tb_op_id or operator_id
                resp_data = await self._tb_request(
                    "PUT",
                    f"/v3/task/{task_id}/executor",
                    json={"executorId": tb_native_assignee},
                    operator_id=op_id,
                )
                result["executorId"] = tb_native_assignee
                logger.info("TB API 设置执行者成功: taskId=%s, executor=%s, resp=%s",
                            task_id, tb_native_assignee, str(resp_data)[:200])
            except Exception as e:
                logger.error("TB API 设置执行者失败: %s", e)

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
        result = data.get("result", [])
        logger.info("query_project_tasks uid=%s pid=%s 返回 %d 个任务", operator_id, pid, len(result))
        return result

    async def get_task_by_unique_id(
        self,
        unique_id: int,
        operator_id: str,
        project_id: Optional[str] = None,
    ) -> Optional[dict]:
        """通过 uniqueId（如 BP3-108 中的 108）全项目搜索任务。

        策略: 先用 query_project_tasks 快速匹配（列表API会返回 uniqueId 则命中），
        若未找到则逐个调用 get_task_detail_tb 补查 uniqueId（历史任务）。
        """
        import asyncio
        pid = project_id or self._settings.teambition_default_project_id

        # 快速路径: 列表 API 返回 uniqueId 时可直接匹配
        all_tasks = await self.query_project_tasks(operator_id, pid)
        task = next((t for t in all_tasks if t.get("uniqueId") == unique_id), None)
        if task:
            logger.info("列表快速匹配 uniqueId=%d: taskId=%s", unique_id, task.get("taskId"))
            return task

        if not all_tasks:
            logger.warning("项目无任务，无法定位 uniqueId=%d", unique_id)
            return None

        # 慢速路径: 逐个调用详情 API（列表 API 不含 uniqueId 时的历史任务回退）
        logger.info("列表 API 未返回 uniqueId，逐个调用详情 API 定位 uniqueId=%d（共 %d 个任务）",
                    unique_id, len(all_tasks))

        async def _get_detail(t: dict):
            tid = t.get("taskId", "")
            if not tid:
                return None
            detail = await self.get_task_detail_tb(tid, operator_id)
            if detail and detail.get("uniqueId") == unique_id:
                return {**t, **detail}
            return None

        results = await asyncio.gather(*[_get_detail(t) for t in all_tasks])
        task = next((r for r in results if r), None)
        if task:
            logger.info("详情补查命中 uniqueId=%d: taskId=%s", unique_id, task.get("taskId"))
        else:
            logger.warning("全量详情查询未找到 uniqueId=%d", unique_id)
        return task

    async def search_task_by_title(
        self,
        title: str,
        operator_id: str,
        project_id: Optional[str] = None,
        task_id_map: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        通过标题搜索项目中的任务

        先拉取项目任务列表，然后按标题模糊匹配
        返回匹配度最高的任务，或 None
        """
        import re as _re

        # 如果 title 是 "BP3-104" 格式，优先从缓存里直接按 taskId 查
        uid_match = _re.fullmatch(r'[A-Za-z0-9]+-(\d+)', title.strip())
        if uid_match:
            uid = int(uid_match.group(1))
            # 1. 内存缓存（本次启动期间创建的任务）
            if task_id_map:
                cached_task_id = task_id_map.get(title.strip())
                if cached_task_id:
                    logger.info("从缓存命中任务ID: %s -> %s", title, cached_task_id)
                    task = await self.get_task_detail_tb(cached_task_id, operator_id)
                    if task:
                        return task
                    logger.warning("缓存命中但任务详情获取失败: %s", cached_task_id)
            # 2. TB 全项目搜索（不受负责人限制）
            task = await self.get_task_by_unique_id(uid, operator_id, project_id)
            if task:
                return task
            # 3. 降级：在"我的任务"里按 uniqueId 匹配
            tasks = await self.query_project_tasks(operator_id, project_id)
            logger.info("降级搜索任务 '%s'，项目中共 %d 个任务", title, len(tasks))
            for t in tasks:
                if t.get("uniqueId") == uid:
                    logger.info("降级匹配到任务ID: %s (id=%s)", title, t.get("taskId"))
                    return t
            logger.warning("未找到任务ID: '%s'", title)
            return None

        tasks = await self.query_project_tasks(operator_id, project_id)

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

    async def update_task_scenario_field_config(
        self, task_id: str, operator_id: str, scenario_field_config_id: str,
    ) -> dict:
        """
        更新任务的任务类型

        API: PUT https://open.teambition.com/api/v3/task/{taskId}/sfc/update
        Body: {"sfcId": "<目标任务类型ID>"}
        参考: teambition/openapi-sdk-golang UpdateTaskSfcV3
        """
        tb_user_id = await self.resolve_tb_member_id(operator_id)
        op_id = tb_user_id or operator_id
        data = await self._tb_request(
            "PUT",
            f"/v3/task/{task_id}/sfc/update",
            json={"sfcId": scenario_field_config_id},
            operator_id=op_id,
        )
        error_code = data.get("errorCode", "") if isinstance(data, dict) else ""
        if error_code:
            error_msg = data.get("errorMessage", "")
            raise RuntimeError(f"更新任务类型失败: {error_msg} (errorCode={error_code})")
        logger.info("任务 %s 类型已更新为 %s", task_id, scenario_field_config_id)
        return data.get("result", data)

    # ============================================================
    # 自定义字段 (Custom Fields)
    # ============================================================

    async def get_customfield_definitions(
        self, operator_id: str, project_id: Optional[str] = None,
    ) -> list[dict]:
        """
        获取项目的自定义字段定义列表

        API: GET https://open.teambition.com/api/v3/project/{projectId}/customfield/search
        返回: [{"_id": "xxx", "name": "需求来源", "type": "commongroup", "choices": [...], ...}]
        """
        pid = project_id or self._settings.teambition_default_project_id
        tb_user_id = await self.resolve_tb_member_id(operator_id)
        op_id = tb_user_id or operator_id
        try:
            data = await self._tb_request(
                "GET",
                f"/v3/project/{pid}/customfield/search",
                operator_id=op_id,
            )
            result = data.get("result", [])
            if isinstance(result, dict):
                result = result.get("customfields", []) or result.get("result", []) or []
            if result:
                logger.info("自定义字段样例(第1个): %s", json.dumps(result[0], ensure_ascii=False, default=str)[:1000])
            logger.info("项目自定义字段定义: %s", 
                        [(cf.get('_id') or cf.get('id') or cf.get('cfId') or cf.get('customfieldId'), cf.get('name'), cf.get('type')) for cf in result])
            return result
        except Exception as e:
            logger.error("获取项目自定义字段定义失败: %s", e)
            return []

    async def update_task_customfield_value(
        self, task_id: str, operator_id: str, cf_id: str, value: list,
    ) -> dict:
        """
        更新任务的单个自定义字段值

        API: POST https://open.teambition.com/api/v3/task/{taskId}/customfield/update
        Body: {"customfieldId": "xxx", "value": [{"id": "choiceId", "title": "显示名"}]}
        参考: teambition/openapi-sdk-golang UpdateTaskCusomFieldV3
        """
        tb_user_id = await self.resolve_tb_member_id(operator_id)
        op_id = tb_user_id or operator_id
        data = await self._tb_request(
            "POST",
            f"/v3/task/{task_id}/customfield/update",
            json={"customfieldId": cf_id, "value": value},
            operator_id=op_id,
        )
        logger.info("任务 %s 自定义字段 %s 已更新: %s", task_id, cf_id, value)
        return data.get("result", data)

    async def set_task_custom_field_by_name(
        self, task_id: str, operator_id: str,
        field_name: str, field_value: str,
    ) -> bool:
        """
        根据自定义字段名称和值来设置

        流程:
        1. 获取项目自定义字段定义 -> 找到字段 ID
        2. 如果是选择类型，从 choices 中匹配 value
        3. 如果是成员类型，解析用户 ID
        4. 调用更新接口

        Args:
            field_name: 自定义字段名称, 如 "需求来源"
            field_value: 字段值, 如 "其他" 或 人名
        Returns:
            是否设置成功
        """
        # Step 1: 获取项目自定义字段定义，找到名称匹配的字段
        field_defs = await self.get_customfield_definitions(operator_id)
        target_def = None
        for fd in field_defs:
            fd_name = fd.get("name", "")
            if fd_name == field_name or field_name in fd_name or fd_name in field_name:
                target_def = fd
                logger.info("匹配自定义字段定义: '%s' -> %s (type=%s)", 
                           field_name, fd.get('_id'), fd.get('type'))
                break

        if not target_def:
            logger.warning("未找到自定义字段 '%s'，可用字段: %s", 
                         field_name, [f.get('name') for f in field_defs])
            return False

        cf_id = target_def.get("_id") or target_def.get("id") or target_def.get("cfId") or target_def.get("customfieldId", "")
        cf_type = target_def.get("type", "")
        logger.info("自定义字段 '%s': cfId=%s, type=%s", field_name, cf_id, cf_type)

        # Step 2: 根据字段类型构建值
        cf_value = None
        if cf_type in ("select", "commongroup", "dropDown"):
            # 选择类型: 从 choices 中匹配
            choices = target_def.get("choices", [])
            for choice in choices:
                choice_val = choice.get("value", "")
                choice_id = choice.get("id") or choice.get("_id") or choice.get("choiceId", "")
                if choice_val == field_value or field_value in choice_val or choice_val in field_value:
                    # value 格式: [{"id": "choiceId", "title": "显示名"}]
                    cf_value = [{"id": choice_id, "title": choice_val}]
                    logger.info("匹配选项: '%s' -> %s (%s)", field_value, choice_val, choice_id)
                    break
            if not cf_value:
                logger.warning("字段 '%s' 未找到选项 '%s'，可用选项: %s", 
                             field_name, field_value, [(c.get('value'), c.get('id') or c.get('_id')) for c in choices])
                return False
        elif cf_type in ("member", "members", "lookup"):
            # 成员类型: 解析用户 ID 并转换为 TB userId
            user_id = await self.resolve_user_id(field_value, operator_id=operator_id)
            if user_id:
                tb_uid = await self.resolve_tb_member_id(user_id)
                uid = tb_uid or user_id
                # value 格式: [{"id": "tbUserId", "title": "姓名"}]
                cf_value = [{"id": uid, "title": field_value}]
            else:
                logger.warning("自定义字段 '%s' 未找到用户 '%s'", field_name, field_value)
                return False
        else:
            # 其他类型 (文本等): value 格式: [{"id": "", "title": "值"}]
            cf_value = [{"id": "", "title": field_value}]

        # Step 3: 更新
        try:
            await self.update_task_customfield_value(task_id, operator_id, cf_id, cf_value)
            logger.info("自定义字段 '%s' 已设置为 '%s'", field_name, field_value)
            return True
        except Exception as e:
            logger.error("设置自定义字段 '%s' 失败: %s", field_name, e)
            return False

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
    ) -> tuple[Optional[str], str]:
        """根据迭代名称查找迭代 ID，返回 (sprint_id, actual_name)。
        actual_name 为 API 中的真实迭代名称；未找到时 sprint_id=None，actual_name=sprint_name。
        """
        import re as _re

        def _normalize(s: str) -> str:
            return _re.sub(r"[\s\-_·•]", "", s).lower()

        sprints = await self.get_project_sprints(operator_id, project_id)
        norm_input = _normalize(sprint_name)
        # 第一轮：精确匹配或原始子串匹配
        for s in sprints:
            name = s.get("name", "")
            if name == sprint_name or sprint_name in name:
                logger.info("匹配迭代(精确): '%s' -> '%s' (%s)", sprint_name, name, s.get("sprintId"))
                return s.get("sprintId", ""), name
        # 第二轮：normalize 后子串匹配（忽略连字符/空格差异）
        for s in sprints:
            name = s.get("name", "")
            norm_name = _normalize(name)
            if norm_input in norm_name or norm_name in norm_input:
                logger.info("匹配迭代(模糊): '%s' -> '%s' (%s)", sprint_name, name, s.get("sprintId"))
                return s.get("sprintId", ""), name
        logger.warning("未找到迭代: '%s'", sprint_name)
        return None, sprint_name

    async def resolve_sprint_name_by_id(
        self, sprint_id: str, operator_id: str, project_id: Optional[str] = None,
    ) -> str:
        """根据 sprintId 返回迭代名称，未找到时返回空字符串"""
        if not sprint_id:
            return ""
        sprints = await self.get_project_sprints(operator_id, project_id)
        for s in sprints:
            if s.get("sprintId") == sprint_id:
                return s.get("name", "")
        return ""

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

    # ============================================================
    # 附件上传
    # ============================================================

    async def upload_attachment_to_task(
        self,
        task_id: str,
        operator_id: str,
        file_name: str,
        file_bytes: bytes,
        file_type: str = "image/png",
        project_id: Optional[str] = None,
    ) -> dict:
        """
        上传附件到 Teambition 任务

        流程:
        1. POST /v3/awos/upload-token  → 获取上传凭证 + uploadUrl + token
        2. PUT uploadUrl               → 上传文件字节
        3. POST /v3/work/create         → 创建文件记录关联到项目

        参考: teambition/openapi-sdk-golang FileAPI
        """
        pid = project_id or self._settings.teambition_default_project_id

        # 将钉钉 userId 转为 Teambition userId（开放平台 API 需要 TB userId 作为 x-operator-id）
        tb_operator_id = await self.resolve_tb_member_id(operator_id)
        if not tb_operator_id:
            raise RuntimeError(f"无法将钉钉 userId {operator_id} 转为 Teambition userId")

        # Step 1: 获取上传凭证
        token_data = await self._tb_request(
            "POST",
            "/v3/awos/upload-token",
            json={
                "scope": f"task:{task_id}",
                "fileName": file_name,
                "fileSize": len(file_bytes),
                "fileType": file_type,
                "category": "attachment",
            },
            operator_id=tb_operator_id,
        )
        result = token_data.get("result", token_data) if isinstance(token_data, dict) else {}
        if isinstance(result, dict):
            upload_url = result.get("uploadUrl", "")
            file_token = result.get("token", "")
        else:
            upload_url = ""
            file_token = ""

        if not upload_url or not file_token:
            raise RuntimeError(
                f"获取上传凭证失败: uploadUrl={upload_url}, token={file_token}, "
                f"response={token_data}"
            )
        logger.info("获取上传凭证成功: fileName=%s, token=%s...", file_name, file_token[:20])

        # Step 2: PUT 上传文件字节到预签名 URL
        # 注意：OSS 预签名 URL 的签名包含 Content-Type，不能随意设置
        async with httpx.AsyncClient(timeout=60.0) as client:
            put_resp = await client.put(
                upload_url,
                content=file_bytes,
            )
            if put_resp.status_code >= 300:
                raise RuntimeError(
                    f"文件上传失败: status={put_resp.status_code}, "
                    f"body={put_resp.text[:200]}"
                )
        logger.info("文件上传成功: %s (%d bytes)", file_name, len(file_bytes))

        # Step 3: 创建文件记录
        work_data = await self._tb_request(
            "POST",
            "/v3/work/create",
            json={
                "projectId": pid,
                "fileTokens": [file_token],
            },
            operator_id=tb_operator_id,
        )
        work_result = work_data.get("result", work_data) if isinstance(work_data, dict) else {}
        # work/create 返回的 result 可能是列表（每个 fileToken 一个 work）
        work_id = None
        if isinstance(work_result, list) and len(work_result) > 0:
            work_id = work_result[0].get("id") or work_result[0].get("workId") or work_result[0].get("_id")
        elif isinstance(work_result, dict):
            work_id = work_result.get("id") or work_result.get("workId") or work_result.get("_id")
        logger.info("文件记录创建成功: workId=%s, fileName=%s", work_id, file_name)

        if not work_id:
            logger.warning("无法获取 workId，跳过任务关联。work_data=%s", work_data)
            return work_result

        # Step 4: 将文件关联到任务（objectlink）
        # linkedData 必须包含 url 字段
        file_url = f"https://www.teambition.com/project/{pid}/works/{work_id}"
        link_data = await self._tb_request(
            "POST",
            f"/v3/task/{task_id}/objectlinks",
            json={
                "linkedId": work_id,
                "linkedType": "work",
                "linkedData": {
                    "title": file_name,
                    "url": file_url,
                },
            },
            operator_id=tb_operator_id,
        )
        logger.info("附件关联任务成功: taskId=%s, workId=%s, fileName=%s", task_id, work_id, file_name)
        return link_data.get("result", link_data) if isinstance(link_data, dict) else link_data

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
