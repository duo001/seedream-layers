"""火山方舟 Agent Plan 图片生成 / 图层拆分调用封装。

只做两件事：拼请求、翻译错误。不做落盘、不保存 Key。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
DEFAULT_MODEL = "doubao-seedream-5-0-pro"

# 页面下拉里的候选模型，允许用户手动改成别的 ID
KNOWN_MODELS = [
    "doubao-seedream-5-0-pro",
    "doubao-seedream-5.0-flash",
]

# 300s：16 个图层的拆分请求可能要 1~3 分钟
TIMEOUT = httpx.Timeout(300.0, connect=20.0)

_ALLOWED_HOSTS = tuple(
    item.strip().lower()
    for item in os.getenv("ARK_ALLOWED_HOSTS", "volces.com,localhost,127.0.0.1").split(",")
    if item.strip()
)


class ArkError(Exception):
    """上游返回的错误，带原始报文方便前端展开排查。"""

    def __init__(self, message: str, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def validate_base_url(url: str) -> str:
    """校验 Base URL，避免本地服务被当成任意请求的跳板。"""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Base URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Base URL 缺少主机名")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_HOSTS):
        raise ValueError(f"Base URL 主机 {host} 不在允许列表内，可通过环境变量 ARK_ALLOWED_HOSTS 放行")
    return url


def _endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/images/generations"


async def _post(base_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(_endpoint(base_url), headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise ArkError(f"请求方舟超时（{TIMEOUT.read}s），请降低分辨率或减少图层数后重试") from exc
    except httpx.HTTPError as exc:
        raise ArkError(f"请求方舟失败：{exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:4000]}

    if resp.status_code >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        message = (error or {}).get("message") if isinstance(error, dict) else None
        if not message and isinstance(data, dict):
            message = data.get("message")
        raise ArkError(message or f"方舟返回 HTTP {resp.status_code}", resp.status_code, data)

    if not isinstance(data, dict):
        raise ArkError("方舟返回了无法解析的响应", resp.status_code, data)
    return data


async def decompose(
    *,
    base_url: str,
    api_key: str,
    model: str,
    image: str,
    prompt: str = "",
    size: str = "auto",
    watermark: bool = False,
    max_images: int | None = None,
    text_recognition: bool = False,
) -> dict[str, Any]:
    """图层拆分：单张图 → 1 张底图 + N 个透明 PNG 图层。"""
    payload: dict[str, Any] = {
        "model": model,
        "image": image,
        "layer_decomposition": True,
        "size": size or "auto",
        "output_format": "png",
        "response_format": "url",
        "watermark": bool(watermark),
    }
    if prompt:
        payload["prompt"] = prompt

    options: dict[str, Any] = {}
    if max_images:
        options["max_images"] = max(2, min(100, int(max_images)))
    if text_recognition:
        options["text_recognition"] = True
    if options:
        payload["layer_decomposition_options"] = options

    return await _post(base_url, api_key, payload)


async def text_to_image(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    size: str = "2K",
    watermark: bool = False,
) -> dict[str, Any]:
    """文生图，返回结果里取 data[0].url / b64_json 继续拆层。"""
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size or "2K",
        "output_format": "png",
        "response_format": "url",
        "watermark": bool(watermark),
    }
    return await _post(base_url, api_key, payload)
