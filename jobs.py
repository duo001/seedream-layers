"""任务落盘：下载图层、生成整画布对齐版、维护 result.json。

方舟返回的图片 URL 只有 24 小时有效期，所以拿到响应后立刻下载到本地，
result.json 里只保存本地相对路径，绝不依赖临时 URL。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _new_job_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _safe_name(name: str | None, fallback: str) -> str:
    text = _ILLEGAL.sub("-", (name or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if not text:
        text = fallback
    return text[:40]


def _parse_size(value: str | None) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$", value or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _open_image(blob: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(blob))
    image.load()
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _ext_of(image: Image.Image) -> str:
    fmt = (image.format or "PNG").upper()
    if fmt == "JPEG":
        return "jpg"
    if fmt in ("PNG", "WEBP", "BMP", "GIF", "TIFF"):
        return fmt.lower()
    return "png"


def _decode_source(source: str | None) -> bytes:
    """兼容 url 与 b64_json 两种返回。"""
    if not source:
        raise ValueError("图层缺少图片数据")
    if source.startswith("data:"):
        _, _, payload = source.partition(",")
        return base64.b64decode(payload)
    if source.startswith("http://") or source.startswith("https://"):
        raise ValueError("_decode_source 只能处理 data URL")
    return base64.b64decode(source)


async def _download(client: httpx.AsyncClient, item: dict[str, Any]) -> bytes:
    url = item.get("url")
    if url:
        if url.startswith("http://") or url.startswith("https://"):
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
        # 允许 data URL / 纯 base64，便于离线自测
        return _decode_source(url)
    return _decode_source(item.get("b64_json"))


def _align_layer(image: Image.Image, bbox: list[int] | None, width: int, height: int) -> Image.Image:
    """把图层按绝对坐标贴到整画布透明底上，坐标系与输出底图一致。

    注意：不能用 paste(..., mask=alpha)，那会把 alpha 通道再乘一次，
    半透明图层会整体变暗；alpha_composite 才是正确的直通 alpha 叠加。
    """
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rgba = image.convert("RGBA")
    if bbox and len(bbox) == 4:
        left, top, right, bottom = (int(value) for value in bbox)
        box_w = max(1, right - left)
        box_h = max(1, bottom - top)
        if (box_w, box_h) != rgba.size:
            rgba = rgba.resize((box_w, box_h), Image.LANCZOS)

        # 模型给出的 bbox 理论上在画布内，做一次裁剪兜底，避免越界报错
        src_left = max(0, -left)
        src_top = max(0, -top)
        dst_left = max(0, left)
        dst_top = max(0, top)
        copy_w = min(rgba.width - src_left, width - dst_left)
        copy_h = min(rgba.height - src_top, height - dst_top)
        if copy_w > 0 and copy_h > 0:
            part = rgba.crop((src_left, src_top, src_left + copy_w, src_top + copy_h))
            canvas.alpha_composite(part, dest=(dst_left, dst_top))
    else:
        canvas.alpha_composite(rgba, dest=(0, 0))
    return canvas


async def create_job(
    response: dict[str, Any],
    *,
    model: str,
    request_meta: dict[str, Any],
) -> dict[str, Any]:
    """把一次拆层响应整理成本地任务目录，返回 result 字典。"""
    data = response.get("data") or []
    if not isinstance(data, list) or not data:
        raise ValueError("方舟未返回任何图层数据")

    base_item = next((item for item in data if int(item.get("z_index") or 0) == 0), data[0])
    canvas_w, canvas_h = _parse_size(base_item.get("size"))

    job_id = _new_job_id()
    job_dir = OUTPUT_DIR / job_id
    (job_dir / "canvas").mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=True
    ) as client:
        blobs = await asyncio.gather(*[_download(client, item) for item in data])

    base_blob = blobs[data.index(base_item)]
    base_image = _open_image(base_blob)
    if not canvas_w or not canvas_h:
        canvas_w, canvas_h = base_image.size
    base_name = f"base.{_ext_of(base_image)}"
    (job_dir / base_name).write_bytes(base_blob)

    layers: list[dict[str, Any]] = []
    used_z: set[int] = set()
    next_z = 1
    for item, blob in zip(data, blobs):
        if item is base_item:
            continue

        z_index = int(item.get("z_index") or 0)
        if z_index <= 0 or z_index in used_z:
            z_index = next_z
        used_z.add(z_index)
        next_z = max(next_z, z_index + 1)

        image = _open_image(blob).convert("RGBA")
        bbox = item.get("bounding_box") or {}
        absolute = bbox.get("absolute")
        name = item.get("name") or f"图层 {z_index}"
        safe = _safe_name(name, f"layer-{z_index:02d}")

        original_name = f"layer_{z_index:02d}.png"
        (job_dir / original_name).write_bytes(_png_bytes(image))

        aligned_name = f"canvas/{z_index:02d}-{safe}.png"
        aligned = _align_layer(image, absolute, canvas_w, canvas_h)
        (job_dir / aligned_name).write_bytes(_png_bytes(aligned))

        layers.append(
            {
                "id": f"l{z_index}",
                "z_index": z_index,
                "name": name,
                "description": item.get("description") or "",
                "file": original_name,
                "aligned_file": aligned_name,
                "size": item.get("size") or f"{image.width}x{image.height}",
                "bbox": {
                    "absolute": absolute or [0, 0, canvas_w, canvas_h],
                    "normalized": bbox.get("normalized"),
                },
                "text_recognition": item.get("text_recognition"),
            }
        )

    layers.sort(key=lambda layer: layer["z_index"])

    result: dict[str, Any] = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "request": request_meta,
        "usage": response.get("usage") or {},
        "canvas": {"w": canvas_w, "h": canvas_h},
        "base": {
            "file": base_name,
            "size": base_item.get("size") or f"{canvas_w}x{canvas_h}",
            "output_format": base_item.get("output_format"),
        },
        "layers": layers,
    }

    (job_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise KeyError(job_id)
    path = OUTPUT_DIR / job_id
    if not path.is_dir():
        raise KeyError(job_id)
    return path


def load_job(job_id: str) -> dict[str, Any]:
    path = job_dir(job_id) / "result.json"
    if not path.is_file():
        raise KeyError(job_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    if not OUTPUT_DIR.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in OUTPUT_DIR.iterdir():
        if not path.is_dir() or not JOB_ID_RE.match(path.name):
            continue
        result_file = path / "result.json"
        if not result_file.is_file():
            continue
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        results.append(
            {
                "job_id": result.get("job_id") or path.name,
                "created_at": result.get("created_at") or "",
                "model": result.get("model") or "",
                "canvas": result.get("canvas") or {},
                "layer_count": len(result.get("layers") or []),
                # 用底图做历史缩略图：图层图大多是透明背景，缩略图看不出来
                "thumb": result.get("base", {}).get("file"),
                "mode": (result.get("request") or {}).get("mode") or "",
            }
        )
    results.sort(key=lambda item: item["created_at"], reverse=True)
    return results[:limit]
