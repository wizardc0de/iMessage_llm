import os
import json
import sqlite3
import subprocess
import time
import threading
import requests
import logging
import hashlib
import signal
from collections import deque
from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime
import re
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
# 恢复 ddgs 导入，作为 tool calling 的搜索后端
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

# 导入imessage_reader模块
from imessage_reader import iMessageReader

# 禁用Flask的默认日志
logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__)
# 禁用Flask的请求日志
app.logger.disabled = True
# 设置日志级别为ERROR，只显示错误信息
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# 配置文件路径
CONFIG_FILE = "config.json"
USER_SESSIONS_FILE = "user_sessions.json"

# 默认配置
DEFAULT_CONFIG = {
    "llm_provider": "bailian",  # bailian 或 dify
    "bailian_api_key": "",  # 百炼 API Key
    "bailian_app_id": "",   # 百炼应用 ID
    "dify_base_url": "https://api.dify.ai/v1",  # Dify 基础 URL
    "dify_api_key": "",  # Dify API Key
    "dify_app_mode": "chat",  # dify 应用类型: chat 或 workflow
    "dify_inputs_key": "query",  # Dify 工作流输入变量名（仅 workflow 模式有效）
    "check_interval": 10,  # 检查新消息的间隔（秒）
    "last_message_id": 0,  # 上次检查的最后一条消息ID
    "is_running": False,  # 是否正在运行检查
    "enable_image_detection": True,  # 是否启用图片链接检测
    "use_file_watcher": True,  # 是否使用文件监控代替轮询
    "force_check_interval": 60,  # 强制检查间隔（秒），即使使用文件监控也会定期检查
    "auto_user_sessions": True,  # 是否自动为每个用户创建会话（始终为true）
    "use_system_watcher": True,  # 是否使用系统级文件监控（更快速）
    "enable_web_search": True,  # 是否启用联网搜索（百炼应用内部处理）
    "system_prompt": "You are a helpful assistant",  # 系统提示词
    "reasoning_effort": "high",  # 推理强度: low, medium, high
    "enable_thinking": True,  # 是否启用思考模式
}

# 全局变量
config = DEFAULT_CONFIG.copy()
check_thread = None
stop_event = threading.Event()
message_reader = None
message_reader_thread = None
processing_lock = threading.Lock()  # 防止并发处理同一批消息

# 日志记录
log_entries = deque(maxlen=100)  # 最多保存100条日志


def add_log(message, level="info"):
    """添加日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {"timestamp": timestamp, "message": message, "level": level}
    log_entries.append(log_entry)

    # 只输出消息处理和回复相关的日志
    if "成功回复消息" in message:
        print(f"成功: {message}")
    elif "处理消息" in message and level == "success":
        print(f"处理: {message}")
    elif "检测到" in message and "条新消息" in message and level == "success":
        print(f"检测: {message}")
    elif level == "error" and ("处理消息错误" in message or "发送消息失败" in message):
        print(f"错误: {message}")


class UserSessionManager:
    """管理iMessage用户的会话信息"""

    def __init__(self, sessions_file):
        self.sessions_file = sessions_file
        self.sessions = {}
        self.load_sessions()

    def load_sessions(self):
        """加载用户会话信息"""
        if os.path.exists(self.sessions_file):
            try:
                with open(self.sessions_file, "r") as f:
                    self.sessions = json.load(f)
                add_log(f"已加载 {len(self.sessions)} 个用户会话", "info")
            except Exception as e:
                add_log(f"加载用户会话失败: {str(e)}", "error")
                self.sessions = {}

    def save_sessions(self):
        """保存用户会话信息"""
        try:
            with open(self.sessions_file, "w") as f:
                json.dump(self.sessions, f, indent=4)
        except Exception as e:
            add_log(f"保存用户会话失败: {str(e)}", "error")

    def get_user_session(self, phone_number):
        """获取用户会话信息，如果不存在则创建"""
        if phone_number not in self.sessions:
            # 为新用户创建会话信息
            user_id = f"imessage-{self._generate_user_id(phone_number)}"
            self.sessions[phone_number] = {
                "user_id": user_id,
                "conversation_id": "",
                "messages": [],
                "last_active": datetime.now().isoformat(),
            }
            add_log(f"为用户 {phone_number} 创建新会话，用户ID: {user_id}", "info")
            self.save_sessions()
        else:
            # 更新最后活动时间
            self.sessions[phone_number]["last_active"] = datetime.now().isoformat()
            # 兼容旧数据：如果没有 messages 字段，初始化为空列表
            if "messages" not in self.sessions[phone_number]:
                self.sessions[phone_number]["messages"] = []
                self.save_sessions()

        return self.sessions[phone_number]

    def add_message(self, phone_number, role, content):
        """添加消息到用户历史"""
        if phone_number in self.sessions:
            self.sessions[phone_number]["messages"].append(
                {
                    "role": role,
                    "content": content,
                    "time": datetime.now().isoformat(),
                }
            )
            # 限制历史长度，保留最近 40 条消息（20 轮对话）
            if len(self.sessions[phone_number]["messages"]) > 40:
                self.sessions[phone_number]["messages"] = self.sessions[phone_number][
                    "messages"
                ][-40:]
            self.sessions[phone_number]["last_active"] = datetime.now().isoformat()
            self.save_sessions()

    def clear_messages(self, phone_number):
        """清空用户的消息历史"""
        if phone_number in self.sessions:
            self.sessions[phone_number]["messages"] = []
            self.sessions[phone_number]["conversation_id"] = ""
            self.sessions[phone_number]["last_active"] = datetime.now().isoformat()
            self.save_sessions()
            add_log(f"已清空用户 {phone_number} 的消息历史", "info")

    def clear_all_sessions(self):
        """清空所有用户会话数据"""
        old_count = len(self.sessions)
        old_sessions = self.sessions.copy()  # 保存一份副本用于日志

        # 记录详细日志
        add_log(f"准备清空所有用户会话数据，当前共有 {old_count} 个会话", "info")
        for phone, session in old_sessions.items():
            add_log(
                f"将删除用户 {phone} 的会话数据，用户ID: {session['user_id']}, 历史消息: {len(session.get('messages', []))} 条",
                "info",
            )

        self.sessions = {}
        self.save_sessions()
        add_log(f"已清空所有用户会话数据，共 {old_count} 个会话", "success")
        return old_count

    def delete_user_session(self, phone_number):
        """完全删除指定用户的会话数据"""
        if phone_number in self.sessions:
            session = self.sessions[phone_number]
            add_log(
                f"准备删除用户 {phone_number} 的会话数据，用户ID: {session['user_id']}, 会话ID: {session.get('conversation_id', '无')}",
                "info",
            )
            del self.sessions[phone_number]
            self.save_sessions()
            add_log(f"已成功删除用户 {phone_number} 的会话数据", "success")
            return True
        else:
            add_log(f"尝试删除不存在的用户 {phone_number} 的会话数据", "warning")
            return False

    def _generate_user_id(self, phone_number):
        """生成唯一的用户ID"""
        # 使用电话号码的哈希值作为用户ID的一部分
        hash_obj = hashlib.md5(phone_number.encode())
        hash_hex = hash_obj.hexdigest()[:8]
        return f"user-{hash_hex}"

    def get_all_sessions(self):
        """获取所有用户会话信息"""
        return self.sessions


# 初始化用户会话管理器
user_session_manager = UserSessionManager(USER_SESSIONS_FILE)


def load_config():
    """加载配置文件"""
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config.update(json.load(f))
    else:
        save_config()


def save_config():
    """保存配置文件"""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def test_bailian_connection():
    """测试百炼应用 API 连接"""
    if not config.get("bailian_api_key"):
        return False, "百炼 API 密钥未设置"
    if not config.get("bailian_app_id"):
        return False, "百炼应用 ID 未设置"

    try:
        headers = {
            "Authorization": f"Bearer {config['bailian_api_key']}",
            "Content-Type": "application/json",
        }
        request_data = {
            "input": {"prompt": "你好"},
            "parameters": {},
            "debug": {},
        }
        response = requests.post(
            f"https://dashscope.aliyuncs.com/api/v1/apps/{config['bailian_app_id']}/completion",
            headers=headers,
            json=request_data,
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            if "output" in data and "text" in data["output"]:
                return True, "百炼应用连接成功"
            else:
                return False, f"响应异常: {json.dumps(data, ensure_ascii=False)}"
        else:
            return False, f"连接失败: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"连接错误: {str(e)}"


def test_dify_connection():
    """测试 Dify API 连接"""
    if not config.get("dify_api_key"):
        return False, "Dify API 密钥未设置"

    try:
        app_mode = config.get("dify_app_mode", "chat")
        if app_mode == "workflow":
            url = f"{config['dify_base_url'].rstrip('/')}/workflows/run"
            request_data = {
                "inputs": {config.get("dify_inputs_key", "query"): "你好"},
                "response_mode": "streaming",
                "user": "test-user",
            }
        else:
            url = f"{config['dify_base_url'].rstrip('/')}/chat-messages"
            request_data = {
                "inputs": {},
                "query": "你好",
                "response_mode": "streaming",
                "user": "test-user",
            }
        headers = {
            "Authorization": f"Bearer {config['dify_api_key']}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=request_data, stream=True, timeout=(10, 30))
        response.raise_for_status()
        answer, _, error_msg = _stream_dify_response(response, app_mode)
        if error_msg:
            return False, f"连接失败: {error_msg}"
        mode_label = "工作流" if app_mode == "workflow" else "对话"
        return True, f"Dify {mode_label}应用连接成功"
    except Exception as e:
        return False, f"连接错误: {str(e)}"


def _parse_dify_output(data):
    """解析 Dify 响应，提取回答文本"""
    if "answer" in data and isinstance(data["answer"], str):
        return data["answer"]

    if "data" in data and "outputs" in data["data"]:
        outputs = data["data"]["outputs"]
        if isinstance(outputs, str):
            return outputs
        if isinstance(outputs, dict):
            for key in ["text", "answer", "result", "output", "reply"]:
                if key in outputs and isinstance(outputs[key], str):
                    return outputs[key]
            # 取第一个字符串值
            for v in outputs.values():
                if isinstance(v, str):
                    return v

    return json.dumps(data, ensure_ascii=False)


def _stream_dify_response(response, app_mode):
    """解析 Dify SSE 流，返回 (answer, conversation_id, error)"""
    answer_parts = []
    conversation_id = ""
    error_msg = ""

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                event_data = json.loads(data_str)
                event_type = event_data.get("event")

                if event_type == "message":
                    # Chat 模式的消息增量
                    msg = event_data.get("answer", "")
                    if msg:
                        answer_parts.append(msg)
                    cid = event_data.get("conversation_id")
                    if cid:
                        conversation_id = cid

                elif event_type == "workflow_finished":
                    # Workflow 模式结束事件
                    outputs = event_data.get("data", {}).get("outputs", {})
                    if isinstance(outputs, dict):
                        for key in ["text", "answer", "result", "output", "reply"]:
                            if key in outputs and isinstance(outputs[key], str):
                                answer_parts.append(outputs[key])
                                break
                        else:
                            for v in outputs.values():
                                if isinstance(v, str):
                                    answer_parts.append(v)
                                    break
                    elif isinstance(outputs, str):
                        answer_parts.append(outputs)

                elif event_type == "error":
                    error_msg = event_data.get("message", "未知错误")

            except json.JSONDecodeError:
                continue

    return "".join(answer_parts), conversation_id, error_msg


def _call_dify_api(message_text, phone_number=None):
    """调用 Dify API（使用 streaming 模式避免 blocking 卡死）"""
    headers = {
        "Authorization": f"Bearer {config['dify_api_key']}",
        "Content-Type": "application/json",
    }
    app_mode = config.get("dify_app_mode", "chat")
    user_id = ""
    conversation_id = ""

    if phone_number:
        user_session = user_session_manager.get_user_session(phone_number)
        user_id = user_session.get("user_id", "")
        conversation_id = user_session.get("conversation_id", "")

    user_id = user_id or "default-user"

    if app_mode == "workflow":
        url = f"{config['dify_base_url'].rstrip('/')}/workflows/run"
        inputs_key = config.get("dify_inputs_key", "query")
        request_data = {
            "inputs": {inputs_key: message_text},
            "response_mode": "streaming",
            "user": user_id,
        }
    else:
        url = f"{config['dify_base_url'].rstrip('/')}/chat-messages"
        request_data = {
            "inputs": {},
            "query": message_text,
            "response_mode": "streaming",
            "conversation_id": conversation_id,
            "user": user_id,
        }

    add_log(f"请求 Dify {app_mode} streaming API: {url}, user={user_id}, msg_len={len(message_text)}", "info")
    start_time = time.time()
    try:
        # streaming 模式下用 stream=True，timeout=(连接, 单次读取)
        # 单次读取 60s：只要服务器持续推送事件就不会超时
        response = requests.post(url, headers=headers, json=request_data, stream=True, timeout=(10, 60))
        response.raise_for_status()

        answer, new_conversation_id, error_msg = _stream_dify_response(response, app_mode)
        elapsed = time.time() - start_time
        add_log(f"Dify streaming 完成: 耗时={elapsed:.1f}s, answer_len={len(answer)}, error={error_msg or '无'}", "info")

        if error_msg:
            raise Exception(f"Dify 返回错误: {error_msg}")

        # 构造与原来 blocking 模式兼容的数据结构
        data = {"answer": answer}
        if new_conversation_id:
            data["conversation_id"] = new_conversation_id
        return data

    except requests.exceptions.Timeout:
        elapsed = time.time() - start_time
        add_log(f"Dify API streaming 超时(>{elapsed:.0f}s)", "error")
        raise
    except requests.exceptions.HTTPError as e:
        add_log(f"Dify API HTTP错误: {e.response.status_code} - {e.response.text[:300]}", "error")
        raise
    except Exception as e:
        elapsed = time.time() - start_time
        add_log(f"Dify API streaming 异常({elapsed:.1f}s): {str(e)}", "error")
        raise


def process_with_dify(message_text, phone_number=None):
    """使用 Dify 处理消息并获取回复"""
    if not config.get("dify_api_key"):
        add_log("Dify API 未配置，无法处理消息", "error")
        return None

    try:
        data = _call_dify_api(message_text, phone_number=phone_number)
        answer = process_reply_text(_parse_dify_output(data))

        # 保存 Dify 返回的 conversation_id（对话模式下用于维持上下文）
        new_conversation_id = data.get("conversation_id", "")
        if phone_number and new_conversation_id:
            user_session_manager.sessions[phone_number]["conversation_id"] = new_conversation_id
            user_session_manager.save_sessions()

        if phone_number:
            user_session_manager.add_message(phone_number, "user", message_text)
            user_session_manager.add_message(phone_number, "assistant", answer)

        add_log(f"成功处理消息: '{message_text[:30]}...'", "success")
        return answer

    except requests.exceptions.Timeout:
        add_log("Dify API 请求超时", "error")
        return None
    except requests.exceptions.ConnectionError:
        add_log("无法连接到 Dify API", "error")
        return None
    except Exception as e:
        add_log(f"Dify 处理消息错误: {str(e)}", "error")
        return None


def get_imessage_db_path():
    """获取iMessage数据库路径"""
    return os.path.expanduser("~/Library/Messages/chat.db")


def send_imessage(phone_number, message):
    """使用AppleScript发送iMessage"""
    try:
        # 确保消息不包含多余的空行
        message = process_reply_text(message)

        result = subprocess.run(
            ["osascript", "send_message.applescript", phone_number, message],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)


# 处理新消息的回调函数
def on_new_messages(messages):
    """处理从imessage_reader接收到的新消息"""
    print(f"收到 {len(messages)} 条新消息")

    # 使用锁防止并发处理
    if processing_lock.acquire(blocking=False):
        try:
            process_messages(messages)
        finally:
            processing_lock.release()
    else:
        print("已有消息处理任务在进行中，跳过本次处理")


def process_messages(messages):
    """处理消息列表"""
    try:
        if messages:
            add_log(f"检测到 {len(messages)} 条新消息", "success")

        for message in messages:
            # 跳过自己发送的消息
            if message["is_from_me"]:
                continue

            # 记录详细的消息信息
            group_info = (
                f" (群聊: {message['group_chat']})" if message.get("group_chat") else ""
            )
            print(
                f"处理消息: 收到消息: 来自 {message['contact']}{group_info}, 内容: '{message['text'][:30]}...'"
            )
            add_log(
                f"收到消息: 来自 {message['contact']}{group_info}, 内容: '{message['text'][:30]}...'",
                "success",
            )

            # 检查消息是否以 @ 开头，如果不是则跳过处理
            text = message["text"].strip()
            if not text.startswith("@"):
                print(f"处理消息: 消息不以@开头，跳过处理")
                add_log(f"跳过处理，消息不以指定前缀开头", "info")
                continue

            # 去除 @ 前缀后再传递给 AI
            text = text[1:].strip()

            # 使用 AI 处理消息
            print(f"处理消息: 开始处理消息")
            if config.get("llm_provider") == "dify":
                reply = process_with_dify(text, message["contact"])
            else:
                reply = process_with_bailian(text, message["contact"])

            # 只有当成功获取到回复时才发送
            if reply:
                print(f"处理消息: 获取到回复，准备发送")
                success, result = send_imessage(message["contact"], reply)
                if not success:
                    print(f"处理消息: 发送失败: {result}")
                    add_log(f"发送消息失败: {result}", "error")
                else:
                    print(f"处理消息: 成功发送回复给 {message['contact']}")
                    add_log(f"成功回复消息给 {message['contact']}", "success")
            else:
                print(f"处理消息: 处理消息时出错，跳过发送")
                add_log(f"跳过发送回复，因为处理消息时出错", "warning")
    except Exception as e:
        print(f"处理消息: 错误: {str(e)}")
        add_log(f"处理新消息错误: {str(e)}", "error")


# 联网搜索工具定义 (DeepSeek 原生 web_search)
# DeepSeek v4 系列模型内置联网搜索功能，需要配置 tools 参数
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索工具，可获取实时信息、新闻、数据等",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，不填则自动从用户问题中提取",
                }
            },
            "required": [],
        },
    },
}

# 获取当前时间工具定义
CURRENT_TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "current_time",
        "description": "获取当前的日期和时间，可用于联网搜索前确认当前日期",
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "日期时间格式，例如 '%Y-%m-%d %H:%M:%S'，可选，默认 '%Y-%m-%d %H:%M:%S'",
                },
                "timezone": {
                    "type": "string",
                    "description": "时区，例如 'Asia/Shanghai'，可选，默认 'Asia/Shanghai'",
                },
            },
            "required": [],
        },
    },
}


def _perform_web_search(query):
    """使用 DuckDuckGo 执行联网搜索"""
    if not query:
        # 未提供关键词时返回提示
        return "未提供搜索关键词，请基于已有信息回答。"

    if DDGS is None:
        return "搜索服务未安装（缺少 ddgs 库），无法执行联网搜索。"

    try:
        add_log(f"开始联网搜索: {query[:80]}...", "info")
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=5)
            if not results:
                return "未找到相关搜索结果，请基于已有信息回答。"

            formatted = []
            for i, r in enumerate(results[:5], 1):
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                formatted.append(f"[{i}] {title}\n{body}\n来源: {href}")
            return "\n\n".join(formatted)
    except Exception as e:
        add_log(f"联网搜索失败: {str(e)}", "error")
        return f"搜索执行失败: {str(e)}，请基于已有信息回答。"


def _call_bailian_api(prompt, session_id=""):
    """调用百炼应用 API"""
    headers = {
        "Authorization": f"Bearer {config['bailian_api_key']}",
        "Content-Type": "application/json",
    }

    request_data = {
        "input": {
            "prompt": prompt,
        },
        "parameters": {},
        "debug": {},
    }

    if session_id:
        request_data["input"]["session_id"] = session_id

    add_log(f"调用百炼应用 API, prompt={prompt[:60]}...", "info")

    try:
        response = requests.post(
            f"https://dashscope.aliyuncs.com/api/v1/apps/{config['bailian_app_id']}/completion",
            headers=headers,
            json=request_data,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_data = response.json()
            error_detail = f", 错误详情: {json.dumps(error_data, ensure_ascii=False)}"
        except:
            error_detail = f", 响应内容: {response.text[:200]}"
        raise Exception(f"百炼API请求失败: {response.status_code}{error_detail}") from e

    add_log("百炼应用 API 调用成功", "success")
    return data


def process_with_bailian(message_text, phone_number=None):
    """使用百炼应用 API 处理消息并获取回复"""
    if not config.get("bailian_api_key"):
        add_log("百炼 API 未配置，无法处理消息", "error")
        return None
    if not config.get("bailian_app_id"):
        add_log("百炼应用 ID 未配置，无法处理消息", "error")
        return None

    try:
        # 获取用户的百炼 session_id
        session_id = ""
        if phone_number:
            user_session = user_session_manager.get_user_session(phone_number)
            session_id = user_session.get("conversation_id", "")

        data = _call_bailian_api(message_text, session_id=session_id)

        if "output" not in data or "text" not in data["output"]:
            add_log(f"百炼响应格式异常: {data}", "error")
            return None

        answer = process_reply_text(data["output"]["text"])

        # 保存新的 session_id
        new_session_id = data["output"].get("session_id", "")
        if phone_number and new_session_id:
            user_session_manager.sessions[phone_number]["conversation_id"] = new_session_id
            user_session_manager.save_sessions()

        # 同时保存消息历史到本地（用于查看）
        if phone_number:
            user_session_manager.add_message(phone_number, "user", message_text)
            user_session_manager.add_message(phone_number, "assistant", answer)

        add_log(f"成功处理消息: '{message_text[:30]}...'", "success")
        return answer

    except requests.exceptions.Timeout:
        add_log("百炼 API 请求超时", "error")
        return None
    except requests.exceptions.ConnectionError:
        add_log("无法连接到百炼 API", "error")
        return None
    except Exception as e:
        add_log(f"处理消息错误: {str(e)}", "error")
        return None


def process_reply_text(text):
    """处理回复文本，删除markdown标记并去除多余的空行"""
    if not text:
        return text

    # 去除开头和结尾的空白字符
    text = text.strip()

    # 删除所有markdown标记
    # 1. 标题 #
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 2. 粗体 ** 或 __
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    # 3. 斜体 * 或 _
    text = re.sub(r"([*_])(.*?)\1", r"\2", text)
    # 4. 删除线 ~~
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    # 5. 链接 [text](url)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", text)
    # 6. 图片 ![alt](url)
    text = re.sub(r"!\[(.*?)\]\((.*?)\)", r"", text)
    # 7. 代码块 ```
    text = re.sub(r"```.*?\n", "", text)
    text = re.sub(r"```", "", text)
    # 8. 行内代码 `
    text = re.sub(r"`(.*?)`", r"\1", text)
    # 9. 引用块 >
    text = re.sub(r"^>\s+", "", text, flags=re.MULTILINE)
    # 10. 水平规则 *** 或 ---
    text = re.sub(r"^(\*{3,}|-{3,}|_{3,})$", "", text, flags=re.MULTILINE)
    # 11. 列表项 * 或 - 或 + 或数字.
    text = re.sub(r"^[\*-\+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)

    # 将多个连续空行替换为单个空行
    text = re.sub(r"\n\s*\n", "\n\n", text)

    # 删除空行留下的多余空白
    text = re.sub(r"^\s+$", "", text, flags=re.MULTILINE)

    # 再次整理空行，移除连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 确保消息末尾没有多余的换行符
    text = text.rstrip("\n")

    return text


class iMessageDBHandler(FileSystemEventHandler):
    """处理iMessage数据库文件变化的事件处理器"""

    def __init__(self):
        super().__init__()
        self.last_event_time = 0
        self.cooldown = 0.5  # 冷却时间，防止短时间内多次触发

    def on_any_event(self, event):
        """当任何文件事件发生时调用"""
        # 检查是否是iMessage数据库文件
        db_path = get_imessage_db_path()

        # 只处理数据库文件的事件
        if event.src_path.endswith("chat.db"):
            current_time = time.time()

            # 防止短时间内多次触发，使用冷却时间
            if current_time - self.last_event_time < self.cooldown:
                return

            self.last_event_time = current_time
            print(f"文件监控: 检测到数据库文件变化: {event.src_path}")
            add_log(f"检测到数据库文件变化: {event.src_path}", "success")

            # 如果服务未运行或未使用文件监控，则不处理
            if not config["is_running"] or not config["use_file_watcher"]:
                print("文件监控: 服务未运行或未使用文件监控，不处理")
                return

            # 触发消息检查
            print("文件监控: 触发消息检查")
            self._check_messages()

    def on_modified(self, event):
        """当文件被修改时调用"""
        # 此方法会被on_any_event调用，但我们保留它以确保兼容性
        # 特别针对chat.db文件的修改事件
        if event.src_path.endswith("chat.db"):
            current_time = time.time()

            # 防止短时间内多次触发，使用冷却时间
            if current_time - self.last_event_time < self.cooldown:
                return

            self.last_event_time = current_time
            print(f"文件监控: 检测到数据库文件修改: {event.src_path}")
            add_log(f"检测到数据库文件修改: {event.src_path}", "success")

            # 如果服务未运行或未使用文件监控，则不处理
            if not config["is_running"] or not config["use_file_watcher"]:
                print("文件监控: 服务未运行或未使用文件监控，不处理")
                return

            # 触发消息检查
            print("文件监控: 触发消息检查")
            self._check_messages()

    def _check_messages(self):
        """检查新消息（带节流控制）"""
        global last_modified_time
        current_time = time.time()

        # 防止短时间内多次触发，使用更短的间隔
        if current_time - last_modified_time < 0.5:  # 减少到0.5秒
            print("文件监控: 触发过于频繁，跳过")
            return

        last_modified_time = current_time

        # 使用锁防止并发处理
        if processing_lock.acquire(blocking=False):
            try:
                print("文件监控: 开始处理新消息")
                # 不再调用process_new_messages函数
                # 现在使用imessage_reader来处理消息
            finally:
                processing_lock.release()
        else:
            print("文件监控: 已有消息处理任务在进行中")
            # 不记录锁冲突的日志，减少日志量
            pass


def start_message_reader():
    """启动iMessage消息读取器"""
    global message_reader, message_reader_thread

    add_log("尝试启动iMessage消息监控...", "info")

    if message_reader_thread is not None:
        add_log("检测到已存在的消息监控线程，尝试停止...", "warning")
        stop_message_reader()

    try:
        # 创建iMessageReader实例
        add_log("创建iMessageReader实例...", "info")
        message_reader = iMessageReader()

        # 检查数据库访问权限
        add_log("检查数据库访问权限...", "info")
        if not message_reader.check_db_access():
            add_log("无法访问iMessage数据库，请确保已授予权限", "error")
            return False

        # 创建并启动监控线程
        add_log("创建并启动监控线程...", "info")
        message_reader_thread = threading.Thread(
            target=message_reader.monitor_messages, args=(on_new_messages,), daemon=True
        )
        message_reader_thread.start()

        add_log("已启动iMessage消息监控", "success")
        return True
    except Exception as e:
        add_log(f"启动iMessage消息监控失败: {str(e)}", "error")
        message_reader = None
        message_reader_thread = None
        return False


def stop_message_reader():
    """停止iMessage消息读取器"""
    global message_reader, message_reader_thread

    if message_reader_thread is None:
        return

    try:
        # 停止监控线程
        if message_reader:
            # 使用新添加的 stop 方法停止监控
            add_log("正在停止iMessage消息监控...", "info")
            message_reader.stop()

        message_reader = None
        message_reader_thread = None
        add_log("已停止iMessage消息监控", "warning")
    except Exception as e:
        add_log(f"停止iMessage消息监控失败: {str(e)}", "error")


def message_checker():
    """后台线程，定期检查新消息"""
    last_force_check_time = 0
    last_log_time = 0
    last_reader_retry_time = 0
    reader_retry_interval = 300  # 5分钟重试一次消息监控
    log_interval = 3600  # 每小时最多记录一次日志，大幅减少日志量

    while not stop_event.is_set():
        current_time = time.time()

        if config["is_running"]:
            # 如果消息读取器未运行，尝试重新启动
            if (
                message_reader_thread is None
                and (current_time - last_reader_retry_time) > reader_retry_interval
            ):
                print("后台检查: 尝试重新启动消息监控")

                # 确保先停止任何可能仍在运行的监控器
                stop_message_reader()

                # 等待一段时间，确保旧的监控器完全停止
                time.sleep(1)

                if start_message_reader():
                    print("后台检查: 消息监控重启成功")
                else:
                    print("后台检查: 消息监控重启失败")
                last_reader_retry_time = current_time

        # 等待指定的间隔时间
        wait_time = min(5, config["check_interval"])  # 最长等待5秒
        stop_event.wait(wait_time)


@app.route("/")
def index():
    """主页"""
    return render_template("index.html", config=config)


@app.route("/save_config", methods=["POST"])
def save_config_route():
    """保存配置"""
    llm_provider = request.form.get("llm_provider", "bailian")
    bailian_api_key = request.form.get("bailian_api_key", "").strip()
    bailian_app_id = request.form.get("bailian_app_id", "").strip()
    dify_base_url = request.form.get("dify_base_url", "https://api.dify.ai/v1").strip()
    dify_api_key = request.form.get("dify_api_key", "").strip()
    dify_app_mode = request.form.get("dify_app_mode", "chat").strip()
    dify_inputs_key = request.form.get("dify_inputs_key", "query").strip()
    check_interval = int(request.form.get("check_interval", 10))
    force_check_interval = int(request.form.get("force_check_interval", 60))

    # 获取检查模式
    check_mode = request.form.get("check_mode", "polling")
    use_file_watcher = check_mode == "file_watcher"

    # 获取高级选项
    enable_image_detection = "enable_image_detection" in request.form
    use_system_watcher = "use_system_watcher" in request.form
    enable_web_search = "enable_web_search" in request.form
    enable_thinking = "enable_thinking" in request.form

    # 自动用户会话始终启用
    auto_user_sessions = True

    # 检查文件监控模式是否改变
    file_watcher_changed = config["use_file_watcher"] != use_file_watcher
    system_watcher_changed = (
        config.get("use_system_watcher", True) != use_system_watcher
    )

    # 更新配置
    config["llm_provider"] = llm_provider
    config["bailian_api_key"] = bailian_api_key
    config["bailian_app_id"] = bailian_app_id
    config["dify_base_url"] = dify_base_url
    config["dify_api_key"] = dify_api_key
    config["dify_app_mode"] = dify_app_mode
    config["dify_inputs_key"] = dify_inputs_key
    config["check_interval"] = check_interval
    config["force_check_interval"] = force_check_interval
    config["enable_image_detection"] = enable_image_detection
    config["enable_web_search"] = enable_web_search
    config["enable_thinking"] = enable_thinking
    config["auto_user_sessions"] = auto_user_sessions
    config["use_file_watcher"] = use_file_watcher
    config["use_system_watcher"] = use_system_watcher

    save_config()

    # 如果文件监控模式改变，需要重新启动或停止监控
    if (file_watcher_changed or system_watcher_changed) and config["is_running"]:
        stop_message_reader()  # 先停止所有监控
        if use_file_watcher:
            start_message_reader()  # 重新启动监控

    add_log("配置已保存", "success")
    return redirect(url_for("index"))


@app.route("/user_sessions")
def user_sessions():
    """显示所有用户会话信息"""
    sessions = user_session_manager.get_all_sessions()
    return render_template("user_sessions.html", sessions=sessions)


@app.route("/reset_user_session/<phone_number>", methods=["POST"])
def reset_user_session(phone_number):
    """重置指定用户的消息历史"""
    if phone_number in user_session_manager.sessions:
        user_session_manager.clear_messages(phone_number)
        add_log(f"已重置用户 {phone_number} 的消息历史", "success")
    return redirect(url_for("user_sessions"))


@app.route("/delete_user_session/<phone_number>", methods=["POST"])
def delete_user_session(phone_number):
    """删除指定用户的会话数据"""
    add_log(f"收到删除用户 {phone_number} 会话数据的请求", "info")
    success = user_session_manager.delete_user_session(phone_number)
    if success:
        add_log(f"已删除用户 {phone_number} 的会话数据", "success")
    else:
        add_log(f"删除用户 {phone_number} 的会话数据失败，可能不存在", "warning")
    return redirect(url_for("user_sessions"))


@app.route("/clear_all_sessions", methods=["POST"])
def clear_all_sessions():
    """清空所有用户会话数据"""
    add_log(f"收到清空所有用户会话数据的请求", "info")
    count = user_session_manager.clear_all_sessions()
    add_log(f"已清空所有用户会话数据，共 {count} 个会话", "success")
    return redirect(url_for("user_sessions"))


@app.route("/test_connection", methods=["POST"])
def test_connection():
    """测试 AI 平台连接"""
    if config.get("llm_provider") == "dify":
        success, message = test_dify_connection()
    else:
        success, message = test_bailian_connection()
    return jsonify({"success": success, "message": message})


@app.route("/debug_llm", methods=["POST"])
def debug_llm():
    """调试 AI 平台 API，返回详细的请求和响应"""
    message = request.form.get("message", "")
    if not message:
        return jsonify({"success": False, "error": "请输入测试消息"})

    provider = config.get("llm_provider", "bailian")

    try:
        add_log(f"调试请求 ({provider}): {message[:50]}...", "info")

        if provider == "dify":
            if not config.get("dify_api_key"):
                return jsonify({"success": False, "error": "Dify API 密钥未设置"})

            data = _call_dify_api(message, phone_number="debug-user")
            answer = _parse_dify_output(data)

            app_mode = config.get("dify_app_mode", "chat")
            if app_mode == "workflow":
                request_data = {
                    "inputs": {config.get("dify_inputs_key", "query"): message},
                    "response_mode": "streaming",
                    "user": "debug-user",
                }
            else:
                request_data = {
                    "inputs": {},
                    "query": message,
                    "response_mode": "streaming",
                    "conversation_id": "",
                    "user": "debug-user",
                }

            add_log("调试请求成功 (Dify)", "success")
            return jsonify(
                {
                    "success": True,
                    "content": answer,
                    "tool_history": [],
                    "request": request_data,
                    "full_response": data,
                }
            )
        else:
            if not config.get("bailian_api_key"):
                return jsonify({"success": False, "error": "百炼 API 密钥未设置"})
            if not config.get("bailian_app_id"):
                return jsonify({"success": False, "error": "百炼应用 ID 未设置"})

            data = _call_bailian_api(message)

            request_data = {
                "input": {"prompt": message},
                "parameters": {},
                "debug": {},
            }

            if "output" in data and "text" in data["output"]:
                content = data["output"]["text"]
                add_log("调试请求成功", "success")
                return jsonify(
                    {
                        "success": True,
                        "content": content,
                        "tool_history": [],
                        "request": request_data,
                        "full_response": data,
                    }
                )
            add_log(f"调试响应格式异常: {data}", "error")
            return jsonify(
                {
                    "success": False,
                    "error": "响应格式异常",
                    "tool_history": [],
                    "request": request_data,
                    "full_response": data,
                }
            )

    except requests.exceptions.Timeout:
        add_log("调试请求超时", "error")
        return jsonify({"success": False, "error": "请求超时"})
    except requests.exceptions.ConnectionError:
        add_log(f"无法连接到 {provider.upper()} API", "error")
        return jsonify({"success": False, "error": f"无法连接到 {provider.upper()} API 服务器"})
    except Exception as e:
        add_log(f"调试错误: {str(e)}", "error")
        return jsonify({"success": False, "error": str(e)})


@app.route("/get_logs", methods=["GET"])
def get_logs():
    """获取日志"""
    return jsonify(list(log_entries))


@app.route("/clear_logs", methods=["POST"])
def clear_logs():
    """清除日志"""
    log_entries.clear()
    return jsonify({"success": True})


@app.route("/toggle_service", methods=["POST"])
def toggle_service():
    """启动或停止服务"""
    config["is_running"] = not config["is_running"]

    if config["is_running"]:
        # 启动消息监控
        if start_message_reader():
            add_log("服务已启动（使用imessage_reader监控）", "success")
        else:
            add_log("服务已启动但消息监控启动失败，请检查权限设置", "warning")
    else:
        # 停止消息监控
        stop_message_reader()
        add_log("服务已停止", "warning")

    save_config()
    return jsonify({"is_running": config["is_running"]})


@app.route("/get_status", methods=["GET"])
def get_status():
    """获取当前状态"""
    return jsonify(
        {
            "is_running": config["is_running"],
            "last_message_id": config["last_message_id"],
        }
    )


@app.route("/reset_last_message", methods=["POST"])
def reset_last_message():
    """重置最后处理的消息ID"""
    reset_type = request.form.get("reset_type", "zero")

    if reset_type == "latest":
        # 重置为最新消息ID
        try:
            db_path = get_imessage_db_path()
            if not os.path.exists(db_path):
                return jsonify({"success": False, "message": "iMessage数据库不存在"})

            conn = sqlite3.connect(db_path, timeout=5)
            cursor = conn.cursor()

            # 获取最新消息ID
            cursor.execute("SELECT MAX(ROWID) as max_id FROM message")
            max_id_result = cursor.fetchone()

            if max_id_result and max_id_result[0]:
                old_id = config["last_message_id"]
                config["last_message_id"] = max_id_result[0]
                save_config()

                add_log(
                    f"手动重置最后消息ID: {old_id} -> {max_id_result[0]}", "success"
                )
                add_log(f"只会处理ID > {max_id_result[0]} 的新消息", "success")

                return jsonify(
                    {
                        "success": True,
                        "message": f"已重置为最新消息ID: {max_id_result[0]}",
                        "last_message_id": max_id_result[0],
                    }
                )
            else:
                return jsonify({"success": False, "message": "无法获取最新消息ID"})
        except Exception as e:
            return jsonify({"success": False, "message": f"重置失败: {str(e)}"})
    else:
        # 重置为0（处理所有历史消息）
        old_id = config["last_message_id"]
        config["last_message_id"] = 0
        save_config()

        add_log(f"手动重置最后消息ID: {old_id} -> 0（将处理所有历史消息）", "warning")

        return jsonify(
            {
                "success": True,
                "message": "已重置为0，将处理所有历史消息",
                "last_message_id": 0,
            }
        )


@app.route("/force_check", methods=["POST"])
def force_check():
    """强制检查新消息"""
    if not config["is_running"]:
        return jsonify({"success": False, "message": "服务未运行"})

    try:
        # 这个功能在使用imessage_reader时不再需要
        # 因为imessage_reader会自动检测新消息
        return jsonify(
            {
                "success": True,
                "message": "使用imessage_reader自动检测新消息，无需手动检查",
            }
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"检查失败: {str(e)}"})


def start_app():
    """启动应用程序"""
    # 加载配置
    load_config()

    # 确保消息监控是停止状态
    global stop_event, check_thread

    # 启动消息检查线程
    stop_event = threading.Event()
    check_thread = threading.Thread(target=message_checker)
    check_thread.daemon = True
    check_thread.start()

    # 如果服务已启用，启动消息监控
    if config["is_running"]:
        if not start_message_reader():
            add_log("启动消息监控失败，请检查权限设置", "error")

    # 启动Flask应用，禁用日志输出
    provider_name = config.get("llm_provider", "bailian").upper()
    print(f"iMessage-{provider_name} 服务已启动，访问 http://127.0.0.1:8888 进行配置")
    app.run(host="0.0.0.0", port=8888, debug=False, use_reloader=False)


def cleanup():
    """清理资源"""
    stop_event.set()
    if check_thread:
        check_thread.join(timeout=1)
    stop_message_reader()


if __name__ == "__main__":
    try:
        start_app()
    finally:
        cleanup()
