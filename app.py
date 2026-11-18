"""seedream-layers —— 图片 → 分层图片 的本地工作台（火山方舟 Agent Plan）。

启动：
    python -m uvicorn app:app --host 127.0.0.1 --port 8787
或：
    python app.py

Key 只在请求期间存在于内存，不落盘、不写日志；前端保存在浏览器 localStorage。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

import ark_client
import exporter
import jobs

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
APP_VERSION = "1.0.0"

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MIN_PIXELS = 512 * 512
MAX_PIXELS = 6000 * 6000
DECOMPOSE_SIZES = {"auto", "1K", "1.5K", "2K"}

jobs.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="seedream-layers", version=APP_VERSION, docs_url="/api/docs")
app.mount("/files", StaticFiles(directory=str(jobs.OUTPUT_DIR)), name="files")


class GenerateBody(BaseModel):
    baseUrl: str = ark_client.DEFAULT_BASE_URL
    baseKey: str = ""
    model: str = ark_client.DEFAULT_MODEL
    prompt: str = Field(..., min_length=1)
    layerPrompt: str = ""
    size: str = "2K"
    watermark: bool = False
    maxImages: int = 0


def _prepare_image(raw: bytes) -> str:
    """校验上传图片，并转成方舟接受的 data URL。"""
    if not raw:
        raise HTTPException(400, "上传的图片为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"图片 {len(raw) / 1024 / 1024:.1f}MB，超过 30MB 限制")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:  # noqa: BLE001 - 交给用户的可读提示
        raise HTTPException(400, f"无法解析图片：{exc}") from exc

    fmt = (image.format or "").upper()
    if fmt not in ("PNG", "JPEG"):
        raise HTTPException(400, f"图层拆分仅支持 PNG / JPEG，当前为 {fmt or '未知格式'}")

    width, height = image.size
    pixels = width * height
    if pixels < MIN_PIXELS:
        raise HTTPException(400, f"图片 {width}×{height} 总像素过小，要求 ≥ 512×512")
    if pixels > MAX_PIXELS:
        raise HTTPException(400, f"图片 {width}×{height} 总像素过大，要求 ≤ 6000×6000")
    ratio = width / height
    if ratio < 1 / 16 or ratio > 16:
        raise HTTPException(400, f"宽高比 {ratio:.2f} 超出 1/16 ~ 16 的范围")

    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _check_request(base_url: str, api_key: str, model: str, size: str) -> tuple[str, str, str]:
    key = (api_key or "").strip()
    if not key:
        raise HTTPException(400, "未提供 API Key，请先在右上角「配置」中填写 Agent Plan 专属 Key")
    name = (model or "").strip()
    if not name:
        raise HTTPException(400, "未提供 Model ID")
    try:
        url = ark_client.validate_base_url(base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if size not in DECOMPOSE_SIZES:
        raise HTTPException(400, f"图层拆分仅支持 size = auto / 1K / 1.5K / 2K，收到 {size}")
    return url, key, name


def _upstream_error(exc: ark_client.ArkError) -> HTTPException:
    return HTTPException(
        502,
        detail={
            "message": str(exc),
            "upstream_status": exc.status,
            "upstream": exc.detail,
        },
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": "seedream-layers",
        "version": APP_VERSION,
        "defaults": {
            "baseUrl": ark_client.DEFAULT_BASE_URL,
            "model": ark_client.DEFAULT_MODEL,
        },
        "models": ark_client.KNOWN_MODELS,
        "sizes": sorted(DECOMPOSE_SIZES),
        "has_psd_export": False,
    }


@app.post("/api/decompose")
async def api_decompose(
    image: UploadFile = File(...),
    baseUrl: str = Form(ark_client.DEFAULT_BASE_URL),
    baseKey: str = Form(""),
    model: str = Form(ark_client.DEFAULT_MODEL),
    size: str = Form("auto"),
    watermark: bool = Form(False),
    maxImages: int = Form(0),
    prompt: str = Form(""),
) -> dict[str, Any]:
    base_url, api_key, model_id = _check_request(baseUrl, baseKey, model, size)
    raw = await image.read()
    data_url = _prepare_image(raw)
    source_width, source_height = Image.open(io.BytesIO(raw)).size

    try:
        response = await ark_client.decompose(
            base_url=base_url,
            api_key=api_key,
            model=model_id,
            image=data_url,
            prompt=(prompt or "").strip(),
            size=size,
            watermark=watermark,
            max_images=maxImages or None,
        )
    except ark_client.ArkError as exc:
        raise _upstream_error(exc) from exc

    try:
        return await jobs.create_job(
            response,
            model=model_id,
            request_meta={
                "mode": "upload",
                "size": size,
                "watermark": bool(watermark),
                "max_images": maxImages or None,
                "prompt": (prompt or "").strip(),
                "base_url": base_url,
                "source_size": f"{source_width}x{source_height}",
            },
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/generate")
async def api_generate(body: GenerateBody) -> dict[str, Any]:
    size = body.size if body.size in DECOMPOSE_SIZES else "auto"
    base_url, api_key, model_id = _check_request(
        body.baseUrl, body.baseKey, body.model, size
    )

    try:
        generated = await ark_client.text_to_image(
            base_url=base_url,
            api_key=api_key,
            model=model_id,
            prompt=body.prompt.strip(),
            size="2K" if size == "auto" else size,
            watermark=body.watermark,
        )
    except ark_client.ArkError as exc:
        raise _upstream_error(exc) from exc

    first = (generated.get("data") or [{}])[0]
    source = first.get("url") or first.get("b64_json")
    if not source:
        raise HTTPException(502, "文生图未返回图片地址")

    try:
        response = await ark_client.decompose(
            base_url=base_url,
            api_key=api_key,
            model=model_id,
            image=source,
            prompt=(body.layerPrompt or "").strip(),
            size=size,
            watermark=body.watermark,
            max_images=body.maxImages or None,
        )
    except ark_client.ArkError as exc:
        raise _upstream_error(exc) from exc

    result = await jobs.create_job(
        response,
        model=model_id,
        request_meta={
            "mode": "text",
            "size": size,
            "watermark": bool(body.watermark),
            "max_images": body.maxImages or None,
            "prompt": body.prompt.strip(),
            "layer_prompt": (body.layerPrompt or "").strip(),
            "base_url": base_url,
        },
        generate_usage=generated.get("usage") or {},
    )
    result["generated_image"] = source
    return result


@app.get("/api/jobs")
def api_jobs() -> dict[str, Any]:
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    try:
        return jobs.load_job(job_id)
    except KeyError as exc:
        raise HTTPException(404, f"任务 {job_id} 不存在") from exc


@app.get("/api/export/zip")
def api_export_zip(job: str) -> FileResponse:
    try:
        path = jobs.job_dir(job)
        result = jobs.load_job(job)
    except KeyError as exc:
        raise HTTPException(404, f"任务 {job} 不存在") from exc

    try:
        zip_path = exporter.build_zip(path, result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打包失败：{exc}") from exc

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"seedream-layers-{job}.zip",
    )


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8787, reload=False)
