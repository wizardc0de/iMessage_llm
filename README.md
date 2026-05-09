# iMessage AI 助手

将 iMessage 与阿里云百炼（Bailian）AI 平台集成，实现自动接收和智能回复 iMessage 消息。

## 功能特点

- **实时消息监控**：使用文件系统事件监控 iMessage 数据库变化，秒级响应新消息
- **AI 智能回复**：基于阿里云百炼应用 API 处理消息，支持多轮对话上下文
- **多用户会话隔离**：为每个联系人独立维护会话历史，互不干扰
- **触发机制灵活**：消息以 `@` 开头时触发 AI 回复，避免误触
- **联网搜索支持**：可选启用 DuckDuckGo 联网搜索，获取实时信息
- **Markdown 自动清理**：AI 回复自动去除 Markdown 格式，适配 iMessage 纯文本
- **群聊支持**：支持识别群聊消息并正确回复
- **Web 管理界面**：基于 Flask 的简洁配置面板，支持日志查看和会话管理

## 系统要求

- **macOS**（必须，需要 iMessage 和 AppleScript 支持）
- Python 3.8+
- 终端应用需要**完全磁盘访问权限**（用于读取 `~/Library/Messages/chat.db`）

## 安装

1. 克隆或下载此仓库
2. 安装依赖并启动：

```bash
chmod +x run.sh
./run.sh
```

或手动创建虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## 快速开始

1. 启动应用后，在浏览器中访问 http://localhost:8888
2. 在配置页面填写**百炼 API Key** 和**应用 ID**
3. 点击**测试连接**验证配置是否正确
4. 点击**启动服务**开始监听新消息
5. 用其他设备向你的 iMessage 发送以 `@` 开头的消息，即可收到 AI 回复

## 授予磁盘访问权限

要读取 iMessage 数据库，必须授予终端应用完全磁盘访问权限：

1. 打开**系统设置** → **隐私与安全性** → **完全磁盘访问权限**
2. 点击锁图标并输入密码
3. 点击 **+** 添加你的终端应用（如 Terminal.app 或 iTerm.app）
4. 确保开关为打开状态
5. **重启终端应用**

## 配置百炼应用

1. 前往 [阿里云百炼平台](https://bailian.console.aliyun.com/) 创建应用
2. 在应用详情页获取 **App ID**
3. 在顶部导航获取 **API Key**
4. 将以上信息填入本应用的 Web 配置页面

### 可选配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 检查间隔 | 后台强制检查的间隔（秒） | 10 |
| 强制检查间隔 | 即使使用文件监控也定期全面检查的间隔（秒） | 60 |
| 监控模式 | 文件系统事件监控 / 轮询 | 文件监控 |
| 联网搜索 | 启用 DuckDuckGo 搜索增强回复 | 开启 |
| 思考模式 | 启用深度思考模式（取决于百炼应用配置） | 关闭 |

## 工作原理

1. `imessage_reader.py` 通过 `watchdog` 监控 `~/Library/Messages/chat.db` 的文件变化
2. 当检测到新消息时，筛选以 `@` 开头的消息内容
3. 去除 `@` 前缀后，调用阿里云百炼应用 API 获取 AI 回复
4. 通过 `osascript` 执行 AppleScript 自动发送 iMessage 回复
5. 每个联系人的对话历史和 `session_id` 独立保存在 `user_sessions.json` 中

## 项目结构

```
.
├── app.py                  # Flask 主应用和 API 逻辑
├── imessage_reader.py      # iMessage 数据库监控模块
├── send_message.applescript # 发送 iMessage 的 AppleScript
├── config.json             # 应用配置文件（自动创建）
├── user_sessions.json      # 用户会话数据（自动创建）
├── requirements.txt        # Python 依赖
├── run.sh                  # 一键启动脚本
├── templates/
│   ├── index.html          # 配置管理主页
│   └── user_sessions.html  # 用户会话管理页
└── README.md
```

## 用户会话管理

访问 http://localhost:8888/user_sessions 可以：

- 查看所有联系人的会话历史和消息记录
- 重置单个用户的对话上下文
- 删除单个用户的会话数据
- 一键清空所有用户会话

## 注意事项

- 本应用**仅能在 macOS 上运行**
- 必须授予终端**完全磁盘访问权限**
- 消息必须以 `@` 开头才会触发 AI 回复
- 自己发送的消息不会被处理，避免循环回复
- 首次运行建议重置消息 ID 为最新，避免处理大量历史消息

## 故障排除

| 问题 | 解决方案 |
|------|----------|
| 无法访问 iMessage 数据库 | 检查系统设置中是否授予终端完全磁盘访问权限，并重启终端 |
| 百炼连接测试失败 | 确认 API Key 和 App ID 是否正确，网络是否正常 |
| 消息未被处理 | 确认消息以 `@` 开头；尝试点击**重置消息 ID** |
| 监控线程频繁重启 | 检查 `app.log` 日志文件查看详细错误信息 |
| AppleScript 发送失败 | 确保已在**系统设置** → **隐私与安全性**中允许终端控制 Mac |

## 依赖

- [Flask](https://flask.palletsprojects.com/) — Web 框架
- [requests](https://requests.readthedocs.io/) — HTTP 请求
- [watchdog](https://python-watchdog.readthedocs.io/) — 文件系统监控
- [pandas](https://pandas.pydata.org/) — iMessage 数据库查询
- [ddgs](https://github.com/deedy5/duckduckgo-search) — DuckDuckGo 搜索
