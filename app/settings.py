# -*- coding: utf-8 -*-
"""
Epic Kiosk 配置模块
支持 SiliconFlow / OpenAI 兼容格式 API
"""
import os
import re
import sys
import asyncio
import base64
import json
from pathlib import Path
from typing import Any, List, Union

# === 引入所需库 ===
from hcaptcha_challenger.agent import AgentConfig
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from loguru import logger

# --- 核心路径定义 ---
PROJECT_ROOT = Path(__file__).parent
VOLUMES_DIR = PROJECT_ROOT.joinpath("volumes")
LOG_DIR = VOLUMES_DIR.joinpath("logs")
USER_DATA_DIR = VOLUMES_DIR.joinpath("user_data")
RUNTIME_DIR = VOLUMES_DIR.joinpath("runtime")
RECORD_DIR = VOLUMES_DIR.joinpath("record")


def extract_chat_completion_text(result: dict) -> str:
    """Extract text from an OpenAI-compatible chat completion response."""
    choices = result.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    return ""


def captcha_output_budget(requested_max_tokens: int, minimum: int = 8192) -> int:
    """Keep captcha reasoning from consuming the entire completion budget."""
    return max(requested_max_tokens, minimum)


def captcha_temperature(value: Any, maximum: float = 0.2) -> float:
    """Use deterministic captcha output even when a provider omits temperature."""
    return min(value, maximum) if isinstance(value, (int, float)) else maximum


def captcha_drag_response_is_valid(parsed_response: Any, minimum_distance: float = 8) -> bool:
    """Reject drag answers that do not move the object to a distinct target."""
    if hasattr(parsed_response, "model_dump"):
        parsed_response = parsed_response.model_dump()
    if not isinstance(parsed_response, dict):
        return False

    paths = parsed_response.get("paths")
    if not isinstance(paths, list) or not paths:
        return False

    challenge_prompt = str(parsed_response.get("challenge_prompt") or "").casefold()
    single_path_puzzle = (
        ("pipe" in challenge_prompt and "route" in challenge_prompt)
        or "place where it fits" in challenge_prompt
    )
    if single_path_puzzle and len(paths) != 1:
        return False

    for path in paths:
        if hasattr(path, "model_dump"):
            path = path.model_dump()
        if not isinstance(path, dict):
            continue
        start = path.get("start_point") or path.get("from")
        end = path.get("end_point") or path.get("to")
        if hasattr(start, "model_dump"):
            start = start.model_dump()
        if hasattr(end, "model_dump"):
            end = end.model_dump()
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        try:
            distance_squared = (
                (float(end["x"]) - float(start["x"])) ** 2
                + (float(end["y"]) - float(start["y"])) ** 2
            )
        except (KeyError, TypeError, ValueError):
            continue
        if distance_squared >= minimum_distance ** 2:
            return True
    return False

# ==========================================
# API 提供商配置
# ==========================================
# 默认使用 SiliconFlow；保留 API_PROVIDER 仅用于日志和部署标识。
API_PROVIDER = os.getenv("API_PROVIDER", "siliconflow")

# === 配置类定义 ===
class EpicSettings(AgentConfig):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # [基础配置] OpenAI 兼容 API Key
    # 新部署使用 API_KEY；兼容旧版 SILICONFLOW_API_KEY，避免已有 .env 直接失效。
    API_KEY: SecretStr | None = Field(
        default_factory=lambda: os.getenv("API_KEY") or os.getenv("SILICONFLOW_API_KEY"),
        description="OpenAI-compatible API Key",
    )

    # 覆盖父类的 GEMINI_API_KEY，使其变为可选（本项目通过兼容层调用模型）
    GEMINI_API_KEY: SecretStr | None = Field(
        default=SecretStr(""),
        description="Gemini API Key（本项目无需配置）",
    )

    # API 基础地址；新部署使用 API_BASE_URL；兼容旧版 SILICONFLOW_BASE_URL。
    API_BASE_URL: str = Field(
        default_factory=lambda: os.getenv("API_BASE_URL") or os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        description="OpenAI-compatible API base URL",
    )
    API_USER_AGENT: str = Field(
        default_factory=lambda: os.getenv(
            "API_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        ),
        description="User-Agent for OpenAI-compatible API requests",
    )
    API_SERVICE_TIER: str = Field(
        default_factory=lambda: os.getenv("API_SERVICE_TIER", "fast"),
        description="Optional service_tier for OpenAI-compatible API requests",
    )

    # === 全局统一模型配置 ===
    # 兼容旧配置（GEMINI_MODEL 作为默认）
    GEMINI_MODEL: str = Field(
        default=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        description="默认模型名称",
    )

    # === 验证码模型（需要视觉能力）===
    CAPTCHA_MODEL: str = Field(
        default=os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3-VL-32B-Instruct"),
        description="验证码识别模型（主力）",
    )
    CAPTCHA_MODEL_FALLBACK: str = Field(
        default=os.getenv("CAPTCHA_MODEL_FALLBACK", "Qwen/Qwen3-VL-30B-A3B-Instruct"),
        description="验证码识别模型（备用）",
    )

    # === 主力模型（一般文本任务）===
    PRIMARY_MODEL: str = Field(
        default=os.getenv("PRIMARY_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
        description="主力文本模型",
    )
    PRIMARY_MODEL_FALLBACK: str = Field(
        default=os.getenv("PRIMARY_MODEL_FALLBACK", "deepseek-ai/DeepSeek-V4-Pro"),
        description="主力文本模型（备用）",
    )

    # === hcaptcha-challenger 内置模型配置（必须覆盖默认值）===
    # 这些属性会覆盖 AgentConfig 的默认 gemini 模型名称
    CHALLENGE_CLASSIFIER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3-VL-32B-Instruct"),
        description="挑战分类模型",
    )
    IMAGE_CLASSIFIER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3-VL-32B-Instruct"),
        description="图像分类模型 (image_label_binary)",
    )
    SPATIAL_POINT_REASONER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3-VL-32B-Instruct"),
        description="空间点推理模型 (image_label_area_select)",
    )
    SPATIAL_PATH_REASONER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_MODEL", "Qwen/Qwen3-VL-32B-Instruct"),
        description="空间路径推理模型 (image_drag_drop)",
    )

    EPIC_EMAIL: str = Field(default_factory=lambda: os.getenv("EPIC_EMAIL"))
    EPIC_PASSWORD: SecretStr = Field(default_factory=lambda: os.getenv("EPIC_PASSWORD"))
    DISABLE_BEZIER_TRAJECTORY: bool = Field(
        default_factory=lambda: os.getenv("DISABLE_BEZIER_TRAJECTORY", "false").lower()
        in {"1", "true", "yes", "on"}
    )

    # === hcaptcha-challenger 超时配置 ===
    # 验证码响应超时（秒）
    RESPONSE_TIMEOUT: float = Field(
        default=60.0,
        description="验证码响应超时时间（秒）"
    )

    # 禁用 hcaptcha 文件保存（使用 /tmp 临时目录）
    cache_dir: Path = Path("/tmp/hcaptcha/.cache")
    EXECUTION_TIMEOUT: float = Field(
        default_factory=lambda: float(os.getenv("EXECUTION_TIMEOUT", "120"))
    )
    challenge_dir: Path = Path("/tmp/hcaptcha/.challenge")
    captcha_response_dir: Path = Path("/tmp/hcaptcha/.captcha")

    ENABLE_APSCHEDULER: bool = Field(default=True)
    TASK_TIMEOUT_SECONDS: int = Field(default=900)
    LOGIN_RESULT_TIMEOUT_SECONDS: int = Field(
        default_factory=lambda: int(os.getenv("LOGIN_RESULT_TIMEOUT_SECONDS", "180"))
    )
    CHECKOUT_MAX_ATTEMPTS: int = Field(
        default_factory=lambda: int(os.getenv("CHECKOUT_MAX_ATTEMPTS", "3"))
    )
    CHECKOUT_CAPTCHA_TIMEOUT_SECONDS: int = Field(
        default_factory=lambda: int(os.getenv("CHECKOUT_CAPTCHA_TIMEOUT_SECONDS", "75"))
    )
    CHECKOUT_CONFIRM_DELAY_MS: int = Field(
        default_factory=lambda: max(
            0, int(os.getenv("CHECKOUT_CONFIRM_DELAY_MS", "0"))
        )
    )
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_WORKER_CONCURRENCY: int = Field(default=1)
    CELERY_TASK_TIME_LIMIT: int = Field(default=1200)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=900)

    @property
    def user_data_dir(self) -> Path:
        override = os.getenv("EPIC_USER_DATA_DIR", "").strip()
        if override:
            target_ = Path(override).expanduser().resolve()
            target_.mkdir(parents=True, exist_ok=True)
            return target_
        target_ = USER_DATA_DIR.joinpath(self.EPIC_EMAIL)
        target_.mkdir(parents=True, exist_ok=True)
        return target_

settings = EpicSettings()
settings.ignore_request_questions = ["Please drag the crossing to complete the lines"]

# 记录当前配置
logger.info(f"🎯 API 提供商: {API_PROVIDER}")
logger.info(f"🔐 验证码模型: {settings.CAPTCHA_MODEL} (备用: {settings.CAPTCHA_MODEL_FALLBACK})")
logger.info(f"🤖 主力模型: {settings.PRIMARY_MODEL} (备用: {settings.PRIMARY_MODEL_FALLBACK})")

# ==========================================
# OpenAI 兼容 API 补丁
# 注意：部分视觉模型不支持 response_format: json_object
# 解决方案：从响应中提取 JSON 代码块
# ==========================================
def _apply_openai_compatible_patch():
    """
    OpenAI 兼容 API 调用层。

    默认使用 SiliconFlow：
    - Base URL: https://api.siliconflow.cn/v1
    - API Key 获取地址: https://cloud.siliconflow.cn/i/OVI2n57p
    - 视觉和文本模型均通过 /v1/chat/completions 调用
    """
    if not settings.API_KEY:
        logger.warning("⚠️ 未配置 API_KEY，请从 https://cloud.siliconflow.cn/i/OVI2n57p 获取 SiliconFlow API Key")
        return

    try:
        from google import genai
        from google.genai import types
        import httpx

        # 获取 API Key
        if hasattr(settings.API_KEY, 'get_secret_value'):
            api_key = settings.API_KEY.get_secret_value()
        else:
            api_key = str(settings.API_KEY)

        base_url = settings.API_BASE_URL.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url[:-3]

        logger.info(f"🚀 OpenAI 兼容补丁加载中... | 地址: {base_url}")

        # ==========================================
        # 辅助函数：将 Gemini contents 转换为 OpenAI messages
        # ==========================================
        def _convert_gemini_to_openai(contents: List, model: str) -> tuple:
            """
            将 Gemini 格式的 contents 转换为 OpenAI 格式的 messages
            返回: (messages, has_images)
            """
            messages = []
            has_images = False

            for content in contents:
                # 处理字符串类型（简单的文本消息）
                if isinstance(content, str):
                    if content.strip():
                        messages.append({"role": "user", "content": content})

                # 处理 Gemini Content 对象
                elif hasattr(content, 'parts'):
                    text_parts = []
                    image_parts = []

                    for part in content.parts:
                        # 处理文本
                        if hasattr(part, 'text') and part.text:
                            text_parts.append(part.text)

                        # 处理内联图片 (inline_data)
                        if hasattr(part, 'inline_data') and part.inline_data:
                            has_images = True
                            blob = part.inline_data
                            if hasattr(blob, 'data'):
                                if isinstance(blob.data, bytes):
                                    img_data = blob.data
                                else:
                                    img_data = bytes(blob.data)

                                mime_type = getattr(blob, 'mime_type', 'image/png') or 'image/png'
                                b64_data = base64.b64encode(img_data).decode('utf-8')
                                data_url = f"data:{mime_type};base64,{b64_data}"

                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": data_url}
                                })

                        # 处理 file_data（来自 upload 的文件）
                        if hasattr(part, 'file_data') and part.file_data:
                            has_images = True

                    # 构建 OpenAI 消息格式
                    if text_parts or image_parts:
                        msg_content = []
                        if text_parts:
                            combined_text = "\n".join(text_parts)
                            msg_content.append({"type": "text", "text": combined_text})
                        msg_content.extend(image_parts)
                        messages.append({
                            "role": "user",
                            "content": msg_content if len(msg_content) > 1 else (msg_content[0] if msg_content else "")
                        })

                elif hasattr(content, 'role') and hasattr(content, 'parts'):
                    role = 'assistant' if content.role == 'model' else content.role
                    text = " ".join([p.text for p in content.parts if hasattr(p, 'text')])
                    if text:
                        messages.append({"role": role, "content": text})

            return messages, has_images

        # ==========================================
        # 辅助函数：从响应文本中提取 JSON
        # ==========================================
        def _extract_json_from_response(response_text: str, response_schema=None):
            """
            从模型响应中提取 JSON（支持多种格式）

            尝试顺序：
            1. 直接解析整个响应
            2. 提取 ```json 代码块
            3. 提取 ``` 代码块
            4. 提取 { } 范围内的内容
            """
            if not response_text:
                return None

            # 方法 1：直接解析
            try:
                json_data = json.loads(response_text.strip())
                if response_schema:
                    return response_schema(**json_data)
                return json_data
            except (json.JSONDecodeError, Exception):
                pass

            # 方法 2：提取 ```json 代码块
            json_match = re.search(r'```json\s*([\s\S]*?)```', response_text)
            if json_match:
                try:
                    json_data = json.loads(json_match.group(1).strip())
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            # 方法 3：提取 ``` 代码块（无语言标记）
            code_match = re.search(r'```\s*([\s\S]*?)```', response_text)
            if code_match:
                try:
                    json_data = json.loads(code_match.group(1).strip())
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            # 方法 4：提取 { } 范围内的内容
            brace_match = re.search(r'\{[\s\S]*\}', response_text)
            if brace_match:
                try:
                    json_data = json.loads(brace_match.group(0))
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            return None

        # ==========================================
        # 辅助函数：调用 OpenAI API（不使用 JSON mode）
        # ==========================================
        async def _call_openai_api(
            model: str,
            messages: List[dict],
            temperature: float = 0.7,
            max_tokens: int = 4096,
            response_schema=None,
            system_instruction: str = None,
            enable_thinking: bool | None = None,
        ) -> Any:
            """
            调用 OpenAI 兼容 API
            注意：不使用 response_format，因为部分视觉模型不支持
            """
            url = f"{base_url}/v1/chat/completions"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": settings.API_USER_AGENT,
            }

            # 构建消息列表
            final_messages = []

            # 添加 system instruction
            if system_instruction:
                final_messages.append({"role": "system", "content": system_instruction})

            # 如果有 response_schema，在 system 消息中添加格式要求
            if response_schema:
                schema_json = response_schema.model_json_schema()
                schema_str = json.dumps(schema_json, indent=2, ensure_ascii=False)
                schema_instruction = f"""你必须严格按照以下 JSON Schema 格式返回响应。
返回的 JSON 必须包含在 ```json 代码块中。

JSON Schema:
```json
{schema_str}
```

重要：请确保返回有效的 JSON 格式，包含在代码块中。"""
                if getattr(response_schema, "__name__", "") == "ImageDragDropChallenge":
                    schema_instruction += (
                        "\n拖动任务的 start_point 和 end_point 必须是不同位置，"
                        "不要返回零位移路径。所有 start_point 都必须位于右侧或上方"
                        "带 Move 标识的可拖动图块中心，绝不能选择左侧主画布里的已有"
                        "物体、管道、背景或目标轮廓。管道路线题只返回一条路径：选择"
                        "正确的右侧管道候选，end_point 放在两段断开管道之间的空缺"
                        "中心，不能放到已有管道上。轮廓匹配题也只返回一条路径，"
                        "end_point 必须位于精确匹配轮廓的几何中心。"
                    )
                if final_messages and final_messages[0].get("role") == "system":
                    final_messages[0]["content"] += "\n\n" + schema_instruction
                else:
                    final_messages.insert(0, {"role": "system", "content": schema_instruction})

            final_messages.extend(messages)

            payload = {
                "model": model,
                "messages": final_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                # 注意：不使用 response_format，部分视觉模型不支持
            }

            if settings.API_SERVICE_TIER:
                payload["service_tier"] = settings.API_SERVICE_TIER
            if enable_thinking is not None:
                payload["chat_template_kwargs"] = {
                    "enable_thinking": enable_thinking,
                }

            retryable_statuses = {408, 409, 429, 500, 502, 503, 504}
            max_attempts = int(os.getenv("API_MAX_RETRIES", "3"))
            for attempt in range(1, max_attempts + 1):
                try:
                    async with httpx.AsyncClient(timeout=settings.RESPONSE_TIMEOUT) as client:
                        response = await client.post(url, headers=headers, json=payload)

                    if response.status_code == 200:
                        result = response.json()
                        choices = result.get("choices") or []
                        first_choice = choices[0] if choices else {}
                        content = extract_chat_completion_text(result)
                        if content.strip():
                            if choices:
                                first_choice.setdefault("message", {})["content"] = content
                            return result

                        finish_reason = first_choice.get("finish_reason")
                        usage = result.get("usage") or {}
                        error_msg = (
                            "API returned an empty completion: "
                            f"finish_reason={finish_reason}, usage={usage}"
                        )
                        if attempt == max_attempts:
                            logger.error(f"API_ERROR:{error_msg}")
                            raise ValueError(error_msg)
                        logger.warning(
                            f"{error_msg}; retrying attempt {attempt}/{max_attempts}"
                        )
                        await asyncio.sleep(min(2 * attempt, 8))
                        continue

                    body_preview = response.text[:500].replace("\n", " ")
                    error_msg = f"API call failed: {response.status_code} - {body_preview}"
                    if response.status_code not in retryable_statuses or attempt == max_attempts:
                        logger.error(f"API_ERROR:{error_msg}")
                        raise Exception(error_msg)

                    logger.warning(
                        f"API returned {response.status_code}; retrying attempt {attempt}/{max_attempts}"
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    error_msg = f"API request exception: {type(exc).__name__}: {exc}"
                    if attempt == max_attempts:
                        logger.error(f"API_ERROR:{error_msg}")
                        raise Exception(error_msg) from exc
                    logger.warning(f"{error_msg}; retrying attempt {attempt}/{max_attempts}")

                await asyncio.sleep(min(2 * attempt, 8))

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code != 200:
                    error_msg = f"API 调用失败: {response.status_code} - {response.text}"
                    logger.error(f"❌ {error_msg}")
                    raise Exception(error_msg)

                result = response.json()
                return result

        # ==========================================
        # 劫持 Client 初始化
        # ==========================================
        orig_init = genai.Client.__init__
        def new_init(self, *args, **kwargs):
            kwargs['api_key'] = api_key
            kwargs['http_options'] = types.HttpOptions(base_url="https://generativelanguage.googleapis.com")
            current_model = kwargs.get('model', settings.GEMINI_MODEL)
            logger.info(f"🚀 OpenAI 兼容补丁已应用 | 模型: {current_model}")
            orig_init(self, *args, **kwargs)

        genai.Client.__init__ = new_init

        # ==========================================
        # 劫持文件上传（存储到内存缓存）
        # ==========================================
        file_cache = {}

        async def patched_upload(self_files, file, **kwargs):
            """将文件内容存储到内存缓存，返回伪造的文件 ID"""
            if hasattr(file, 'read'):
                content = file.read()
                if asyncio.iscoroutine(content):
                    content = await content
            elif isinstance(file, (str, Path)):
                with open(file, 'rb') as f:
                    content = f.read()
            else:
                content = bytes(file)

            if asyncio.iscoroutine(content):
                content = await content

            file_id = f"sf_{id(content)}_{len(content)}"
            file_cache[file_id] = content
            pass  # 文件缓存日志已移除
            return types.File(name=file_id, uri=file_id, mime_type="image/png")

        genai.files.AsyncFiles.upload = patched_upload

        # ==========================================
        # 劫持 generate_content：核心转换逻辑
        # ==========================================
        orig_generate = genai.models.AsyncModels.generate_content

        async def patched_generate(self_models, model, contents, **kwargs):
            """
            将 Gemini API 调用转换为 OpenAI API 调用
            从响应中提取 JSON 代码块
            支持模型自动切换（验证码任务 vs 普通任务）
            """
            try:
                # 标准化 contents
                normalized = contents if isinstance(contents, list) else [contents]

                # 检查是否有缓存文件需要处理
                has_cached_files = False
                for content in normalized:
                    if hasattr(content, 'parts'):
                        for part in content.parts:
                            if hasattr(part, 'file_data') and part.file_data:
                                file_uri = getattr(part.file_data, 'file_uri', None) or getattr(part.file_data, 'uri', None)
                                if file_uri and file_uri in file_cache:
                                    has_cached_files = True
                                    data = file_cache[file_uri]
                                    if not hasattr(part, 'inline_data') or part.inline_data is None:
                                        part.inline_data = types.Blob(data=data, mime_type="image/png")
                                    else:
                                        part.inline_data.data = data

                # 转换为 OpenAI 格式
                messages, has_images = _convert_gemini_to_openai(normalized, model)

                if not messages:
                    raise ValueError("无法从 contents 中提取有效消息")

                # 判断任务类型并选择合适的模型
                is_captcha_task = has_images or has_cached_files

                if is_captcha_task:
                    selected_model = settings.CAPTCHA_MODEL
                    logger.debug(f"🎯 验证码模型: {selected_model}")
                    temperature = captcha_temperature(
                        getattr(kwargs.get('config', {}), 'temperature', 0.2)
                    )
                else:
                    selected_model = settings.PRIMARY_MODEL

                logger.debug(f"🤖 调用 OpenAI 兼容 API | 模型: {selected_model} | 图片: {is_captcha_task}")

                # 提取配置参数
                config = kwargs.get('config', {})
                if not is_captcha_task:
                    temperature = getattr(config, 'temperature', 0.7) if hasattr(config, 'temperature') else 0.7
                max_tokens = getattr(config, 'max_output_tokens', 4096) if hasattr(config, 'max_output_tokens') else 4096
                if is_captcha_task:
                    minimum_captcha_tokens = int(
                        os.getenv("CAPTCHA_MAX_OUTPUT_TOKENS", "8192")
                    )
                    max_tokens = captcha_output_budget(
                        max_tokens if isinstance(max_tokens, int) else 4096,
                        minimum_captcha_tokens,
                    )

                # 提取 response_schema（结构化输出）
                response_schema = None
                if hasattr(config, 'response_schema'):
                    response_schema = config.response_schema
                    logger.debug(f"📋 检测到 response_schema: {response_schema.__name__ if hasattr(response_schema, '__name__') else response_schema}")

                # 提取 system_instruction
                system_instruction = None
                if hasattr(config, 'system_instruction'):
                    if hasattr(config.system_instruction, 'parts'):
                        for part in config.system_instruction.parts:
                            if hasattr(part, 'text'):
                                system_instruction = part.text
                                break

                # 调用 OpenAI API
                result = await _call_openai_api(
                    model=selected_model,
                    messages=messages,
                    temperature=temperature if isinstance(temperature, (int, float)) else 0.7,
                    max_tokens=max_tokens if isinstance(max_tokens, int) else 4096,
                    response_schema=response_schema,
                    system_instruction=system_instruction,
                    enable_thinking=False if is_captcha_task else None,
                )

                # 提取响应文本
                response_text = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                logger.debug(f"📄 原始响应: {repr(response_text[:300])}")

                # 处理结构化输出
                parsed_response = None
                if response_schema and response_text:
                    parsed_response = _extract_json_from_response(response_text, response_schema)
                    if parsed_response:
                        if (
                            getattr(response_schema, "__name__", "")
                            == "ImageDragDropChallenge"
                            and not captcha_drag_response_is_valid(parsed_response)
                        ):
                            raise ValueError(
                                "captcha model returned an invalid drag path set"
                            )
                        logger.debug(f"✅ JSON 解析成功")
                    else:
                        logger.debug(f"⚠️ JSON 解析失败，返回原始文本")

                # 构建 Gemini 格式的响应
                response = types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            content=types.Content(
                                parts=[types.Part(text=response_text)],
                                role='model'
                            ),
                            finish_reason='STOP'
                        )
                    ]
                )

                # 如果有解析好的结构化响应，设置 parsed 属性
                if parsed_response:
                    response.parsed = parsed_response

                return response

            except Exception as e:
                error_str = str(e)
                logger.error(f"❌ API 调用异常: {error_str}")

                # 尝试使用备用模型重试
                if is_captcha_task:
                    fallback_model = settings.CAPTCHA_MODEL_FALLBACK
                    logger.debug(f"⚠️ 尝试使用备用验证码模型: {fallback_model}")
                else:
                    fallback_model = settings.PRIMARY_MODEL_FALLBACK
                    logger.debug(f"⚠️ 尝试使用备用主力模型: {fallback_model}")

                # 重试一次
                try:
                    result = await _call_openai_api(
                        model=fallback_model,
                        messages=messages,
                        temperature=temperature if isinstance(temperature, (int, float)) else 0.7,
                        max_tokens=max_tokens if isinstance(max_tokens, int) else 4096,
                        response_schema=response_schema,
                        system_instruction=system_instruction,
                        enable_thinking=False if is_captcha_task else None,
                    )

                    response_text = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                    logger.debug(f"📄 备用模型响应: {repr(response_text[:300])}")

                    # 处理结构化输出
                    parsed_response = None
                    if response_schema and response_text:
                        parsed_response = _extract_json_from_response(response_text, response_schema)
                        if parsed_response:
                            if (
                                getattr(response_schema, "__name__", "")
                                == "ImageDragDropChallenge"
                                and not captcha_drag_response_is_valid(parsed_response)
                            ):
                                raise ValueError(
                                    "fallback captcha model returned an invalid drag path set"
                                )
                            logger.debug(f"✅ 备用模型 JSON 解析成功")

                    response = types.GenerateContentResponse(
                        candidates=[
                            types.Candidate(
                                content=types.Content(
                                    parts=[types.Part(text=response_text)],
                                    role='model'
                                ),
                                finish_reason='STOP'
                            )
                        ]
                    )

                    if parsed_response:
                        response.parsed = parsed_response

                    return response

                except Exception as fallback_error:
                    logger.error(f"❌ 备用模型也失败: {fallback_error}")
                    raise

        genai.models.AsyncModels.generate_content = patched_generate
        logger.info("✅ OpenAI 兼容补丁加载成功")

    except Exception as e:
        logger.error(f"❌ 严重：OpenAI 兼容补丁加载失败! 原因: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# 加载 OpenAI 兼容补丁
# ==========================================
_apply_openai_compatible_patch()

# 导出
__all__ = ['settings']
