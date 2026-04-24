# 钉钉 Teambition 任务管理机器人

在钉钉群聊/单聊中，用自然语言管理 Teambition 项目任务。基于 LLM 大模型理解用户意图，自动完成任务的创建、修改、查询、删除等操作。

---

## 功能一览

| 功能 | 示例 |
|------|------|
| **创建任务** | "给吕鑫下周五之前完成首页设计" |
| **批量创建** | "帮我提这几个单子：XXX、YYY、ZZZ，迭代到周更活动0423中" |
| **修改任务** | "把首页设计的优先级改为高" |
| **批量改类型** | "将蔡宇航的工单类型都改为美术" |
| **完成任务** | "把首页设计标记为完成" |
| **重新打开** | "重新打开首页设计" |
| **删除任务** | "删除任务测试任务" |
| **查询任务** | "查看我的任务" / "查看所有逾期任务" |
| **设置状态** | "把首页设计设为进行中" |
| **导出提交代码** | "导出蚩梦觉醒迭代的提交代码" |

支持的任务类型：需求、任务、缺陷、美术、里程碑、风险

---

## 系统架构

```
钉钉用户 ──消息──▶ 钉钉 Stream (WebSocket)
                          │
                          ▼
                   main_stream.py (消息分发)
                          │
                  ┌───────┼───────┐
                  ▼       ▼       ▼
             LLM 解析  Teambition  钉钉回复
           (意图识别)   (API操作)  (Markdown)
```

- **接入方式**：钉钉 Stream 模式（WebSocket 长连接，无需公网 IP）
- **意图识别**：OpenAI 兼容接口（支持通义千问、DeepSeek 等）
- **任务管理**：Teambition Open API
- **通知机制**：创建任务后自动通知项目管理员

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- 钉钉企业内部应用（已开通机器人能力）
- Teambition 阿里云版项目
- LLM API Key（OpenAI / 通义千问 / DeepSeek 等）

### 2. 一键安装

```bash
# 克隆项目
git clone <your-repo-url> dingtalk_bot
cd dingtalk_bot

# 创建虚拟环境并安装依赖
python -m venv venv

# Windows
venv\Scripts\activate
# Linux / macOS
# source venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# 复制示例配置
cp .env.example .env
```

编辑 `.env` 文件，填入以下配置：

```env
# ============================
# 钉钉机器人配置
# ============================
DINGTALK_APP_KEY=你的应用AppKey
DINGTALK_APP_SECRET=你的应用AppSecret
DINGTALK_ROBOT_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx

# ============================
# Teambition 配置
# ============================
TEAMBITION_APP_ID=你的Teambition应用ID
TEAMBITION_APP_SECRET=你的Teambition应用Secret
TEAMBITION_ORG_ID=你的组织ID
TEAMBITION_DEFAULT_PROJECT_ID=你的项目ID
TEAMBITION_DEFAULT_VIEW_ID=你的看板视图ID
TEAMBITION_PROJECT_KEY=项目前缀如BP3

# ============================
# LLM 配置
# ============================
LLM_API_KEY=你的LLM_API_Key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

### 4. 启动机器人

```bash
python main_stream.py
```

启动后即可在钉钉中 @机器人 或私聊机器人使用。

---

## 配置获取指南

### 钉钉配置

1. 登录 [钉钉开放平台](https://open.dingtalk.com/)
2. 进入 **应用开发** → **企业内部开发** → **创建应用**
3. 在应用信息页获取 `AppKey` 和 `AppSecret`
4. 在 **机器人与消息推送** 中：
   - 开启机器人能力
   - 消息接收模式选择 **Stream 模式**
5. 在 **权限管理** 中开通以下权限：
   - 企业内机器人发送消息
   - 个人手机号信息
   - 通讯录个人信息读权限
6. 发布应用并添加机器人到群聊

### Teambition 配置

1. 登录 [Teambition 开放平台](https://open.teambition.com/)
2. 创建应用，获取 `App ID` 和 `App Secret`
3. **组织 ID**：在 Teambition 管理后台 URL 中获取
4. **项目 ID**：打开项目，URL 中 `/project/` 后面的字符串
   - 例：`https://www.teambition.com/project/65xxxxx/tasks/...` → `65xxxxx`
5. **视图 ID**：打开项目看板视图，URL 中 `/view/` 后面的字符串
   - 例：`...tasks/view/67xxxxx/task/...` → `67xxxxx`
6. **项目前缀**：在任何任务详情页左上角查看（如 `BP3-51` 中的 `BP3`）

### LLM 配置

支持所有 OpenAI 兼容接口，以下是常用选项：

| 服务 | base_url | 推荐模型 |
|------|----------|----------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |

---

## 项目结构

```
dingtalk_bot/
├── main_stream.py          # 主入口 (Stream 模式，推荐)
├── main.py                 # 备用入口 (FastAPI Webhook 模式)
├── config.py               # 配置管理 (pydantic-settings)
├── models.py               # Pydantic 数据模型
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量示例
│
├── dingtalk/               # 钉钉集成模块
│   ├── sender.py           #   消息发送 (文本/Markdown/卡片)
│   └── webhook.py          #   消息回调 (签名验证/消息解析)
│
├── llm/                    # LLM 意图解析模块
│   ├── parser.py           #   自然语言 → 结构化意图
│   └── prompts.py          #   系统提示词模板
│
└── teambition/             # Teambition 集成模块
    ├── client.py           #   API 客户端 (任务CRUD/迭代/成员)
    └── webhook.py          #   事件回调处理
```

---

## 两种运行模式

### Stream 模式（推荐）

通过 WebSocket 长连接接收消息，**无需公网 IP 或域名**，适合本地开发和内网部署。

```bash
python main_stream.py
```

### Webhook 模式

需要公网可访问的 HTTP 端点，适合已有服务器的场景。

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

钉钉机器人配置中填写回调地址：`https://your-domain.com/dingtalk/callback`

---

## 使用示例

### 创建任务

```
给吕鑫下周五之前完成首页设计
```

机器人回复：

> **任务创建成功**
> **任务:** 首页设计
> **类型:** 任务
> **负责人:** 吕鑫
> **截止日期:** 2026-04-24
> **优先级:** 普通

### 批量创建

```
帮我提这几个单子，迭代到周更活动0423中
4.14 - 4.28蚩梦侠隐姬如雪岐王女帝返场卡池
4.14 - 4.28苗疆姬如雪Up卡池
4.14 - 4.28限时累消配置
```

每个任务独立回复，格式与单独创建一致。

### 查询任务

```
查看吕鑫的任务
查看蒲公英战第一迭代未完成的任务
```

### 导出提交代码

```
导出蚩梦觉醒迭代的提交代码
```

返回纯文本，方便一键复制到表格：

```
--tbid=BP3-51 --tbtitle=任务标题 --tburl=链接 --user=负责人
--tbid=BP3-52 --tbtitle=任务标题 --tburl=链接 --user=负责人
```

---

## 常见问题

### Q: 启动后提示连接失败？

确认 `DINGTALK_APP_KEY` 和 `DINGTALK_APP_SECRET` 正确，且应用已发布上线。

### Q: 创建任务失败，提示权限不足？

1. 确认 Teambition 应用的 `App ID` / `App Secret` 正确
2. 确认 `TEAMBITION_ORG_ID` 和 `TEAMBITION_DEFAULT_PROJECT_ID` 正确
3. 确认发送消息的钉钉用户是 Teambition 项目成员

### Q: LLM 解析不准确？

- 尝试换用更强的模型（如 `gpt-4o`、`qwen-max`）
- 确保消息表述清晰，包含必要信息（标题、负责人等）

### Q: 如何查看运行日志？

启动后日志直接输出到控制台（stdout），格式为：
```
2026-04-20 10:00:00 [INFO] __main__: 收到消息...
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 运行时 | Python 3.10+ |
| Web 框架 | FastAPI + Uvicorn |
| HTTP 客户端 | httpx (异步) |
| 配置管理 | pydantic-settings |
| 钉钉接入 | dingtalk-stream (WebSocket) |
| LLM 接口 | OpenAI SDK (兼容接口) |
| 数据验证 | Pydantic v2 |
