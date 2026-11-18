"""导出：分层图片 ZIP（含合成图、layers.json、单文件预览页）。

PSD 导出保留接口位：build_psd() 只抛 NotImplementedError，
将来接入时读同一个 result.json 与 aligned_file 即可，前端无需改动。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

BASE_LAYER_NAME = "00-背景.png"


def _compose(result: dict[str, Any], job_path: Path) -> Image.Image:
    canvas_meta = result.get("canvas") or {}
    width = int(canvas_meta.get("w") or 0)
    height = int(canvas_meta.get("h") or 0)
    base_file = (result.get("base") or {}).get("file")
    base_image = Image.open(job_path / base_file).convert("RGBA") if base_file else None
    if not width or not height:
        if base_image is None:
            raise ValueError("任务缺少底图尺寸")
        width, height = base_image.size

    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    if base_image is not None:
        if base_image.size != (width, height):
            base_image = base_image.resize((width, height), Image.LANCZOS)
        canvas.alpha_composite(base_image)

    for layer in sorted(result.get("layers") or [], key=lambda item: item.get("z_index") or 0):
        aligned = layer.get("aligned_file")
        if not aligned:
            continue
        path = job_path / aligned
        if not path.is_file():
            continue
        canvas.alpha_composite(Image.open(path).convert("RGBA"))
    return canvas


def _slim_layers(result: dict[str, Any]) -> dict[str, Any]:
    layers = []
    for layer in result.get("layers") or []:
        aligned = layer.get("aligned_file")
        if not aligned:
            continue
        layers.append(
            {
                "name": layer.get("name"),
                "z_index": layer.get("z_index"),
                "description": layer.get("description") or "",
                "file": f"layers/{Path(aligned).name}",
                "source": f"source/{layer.get('file')}",
                "size": layer.get("size"),
                "bbox": layer.get("bbox") or {},
            }
        )
    return {
        "job_id": result.get("job_id"),
        "created_at": result.get("created_at"),
        "model": result.get("model"),
        "canvas": result.get("canvas") or {},
        "usage": result.get("usage") or {},
        "usage_generate": result.get("usage_generate") or {},
        "usage_total": result.get("usage_total") or {},
        "layers": layers,
    }


def _viewer_html(result: dict[str, Any], slim: dict[str, Any]) -> str:
    canvas = slim.get("canvas") or {}
    width = int(canvas.get("w") or 1024)
    height = int(canvas.get("h") or 1024)

    entries: list[dict[str, Any]] = [
        {
            "name": "背景",
            "file": f"layers/{BASE_LAYER_NAME}",
            "z_index": 0,
            "description": result.get("base", {}).get("size") or "",
        }
    ]
    for layer in slim.get("layers") or []:
        entries.append(
            {
                "name": layer.get("name") or "图层",
                "file": layer.get("file"),
                "z_index": layer.get("z_index") or 0,
                "description": layer.get("description") or "",
            }
        )

    payload = json.dumps(
        {"canvas": {"w": width, "h": height}, "entries": entries}, ensure_ascii=False
    ).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>seedream-layers 图层预览 - {slim.get('job_id') or ''}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #0f1115; color: #e6e8ee; display: flex; height: 100vh; overflow: hidden; }}
  aside {{ width: 260px; flex: 0 0 260px; border-right: 1px solid #23262f; display: flex;
           flex-direction: column; background: #14161c; }}
  aside h1 {{ font-size: 13px; margin: 0; padding: 14px 16px; border-bottom: 1px solid #23262f;
              font-weight: 600; letter-spacing: .5px; color: #9aa3b2; }}
  .list {{ overflow: auto; flex: 1; padding: 8px; }}
  .item {{ display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: 7px; cursor: pointer; }}
  .item:hover {{ background: #1b1e26; }}
  .item img {{ width: 36px; height: 36px; object-fit: contain; border-radius: 5px;
               background: repeating-conic-gradient(#2a2d36 0% 25%, #22252c 0% 50%) 0 0/10px 10px; }}
  .item .meta {{ min-width: 0; }}
  .item .name {{ font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .item .desc {{ font-size: 11px; color: #7b8494; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  main {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
  .bar {{ display: flex; gap: 14px; align-items: center; padding: 10px 16px; border-bottom: 1px solid #23262f;
          font-size: 12px; color: #8b94a4; }}
  .bar b {{ color: #e6e8ee; font-weight: 500; }}
  .bar button {{ margin-left: auto; background: #232733; color: #dfe3ec; border: 1px solid #2f3442;
                 border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 12px; }}
  .stage {{ flex: 1; overflow: auto; display: flex; }}
  .sizer {{ margin: auto; position: relative; flex: 0 0 auto; }}
  .board {{ position: absolute; left: 0; top: 0; transform-origin: 0 0;
            background: repeating-conic-gradient(#2a2d36 0% 25%, #20232a 0% 50%) 0 0/24px 24px;
            box-shadow: 0 12px 40px rgba(0,0,0,.5); }}
  .board img {{ position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none; }}
</style>
</head>
<body>
<aside>
  <h1>图层列表（{len(entries)}）</h1>
  <div class="list" id="list"></div>
</aside>
<main>
  <div class="bar">
    <span>任务 <b id="jobId">{slim.get('job_id') or ''}</b></span>
    <span>尺寸 <b>{width}×{height}</b></span>
    <span>模型 <b>{slim.get('model') or ''}</b></span>
    <button id="toggleAll">全部显隐</button>
  </div>
  <div class="stage" id="stage">
    <div class="sizer" id="sizer"><div class="board" id="board"></div></div>
  </div>
</main>
<script>
const DATA = {payload};
const board = document.getElementById('board');
const sizer = document.getElementById('sizer');
const stage = document.getElementById('stage');
const list = document.getElementById('list');
const W = DATA.canvas.w, H = DATA.canvas.h;
let zoom = 1;

board.style.width = W + 'px';
board.style.height = H + 'px';

DATA.entries.forEach((entry, i) => {{
  const img = document.createElement('img');
  img.src = entry.file;
  img.style.zIndex = String(i);
  img.dataset.index = String(i);
  board.appendChild(img);

  const item = document.createElement('label');
  item.className = 'item';
  item.innerHTML = `<input type="checkbox" checked>
    <img src="${{entry.file}}" alt="">
    <span class="meta"><span class="name">${{entry.name}}</span>
    <span class="desc">z_index ${{entry.z_index}} ${{entry.description}}</span></span>`;
  const box = item.querySelector('input');
  box.addEventListener('change', () => {{ img.style.display = box.checked ? '' : 'none'; }});
  list.appendChild(item);
}});

function fit() {{
  const pad = 48;
  zoom = Math.min((stage.clientWidth - pad) / W, (stage.clientHeight - pad) / H);
  zoom = Math.max(0.02, Math.min(zoom, 1));
  board.style.transform = `scale(${{zoom}})`;
  sizer.style.width = W * zoom + 'px';
  sizer.style.height = H * zoom + 'px';
}}
window.addEventListener('resize', fit);
fit();

document.getElementById('toggleAll').addEventListener('click', () => {{
  const boxes = [...list.querySelectorAll('input')];
  const anyChecked = boxes.some(box => box.checked);
  boxes.forEach((box, i) => {{
    box.checked = !anyChecked;
    board.children[i].style.display = box.checked ? '' : 'none';
  }});
}});
</script>
</body>
</html>
"""


def _usage_line(result: dict[str, Any]) -> str:
    usage = result.get("usage_total") or {}
    images = usage.get("generated_images")
    tokens = usage.get("output_tokens")
    if images is None and tokens is None:
        return ""
    return (
        f"用量：{images or 0} 张图片 / {tokens or 0} 图像 token"
        "（token 为面积折算值，不参与计费）\n"
    )


def _readme(result: dict[str, Any]) -> str:
    canvas = result.get("canvas") or {}
    return (
        "seedream-layers 分层导出包\n"
        "===========================\n\n"
        f"任务 ID：{result.get('job_id')}\n"
        f"生成模型：{result.get('model')}\n"
        f"生成时间：{result.get('created_at')}\n"
        f"画布尺寸：{canvas.get('w')}×{canvas.get('h')}\n"
        f"图层数量：{len(result.get('layers') or [])} + 1 张底图\n"
        f"{_usage_line(result)}\n"
        "目录说明\n"
        "--------\n"
        "composite.png   合成图，可直接预览或交付\n"
        "layers/         整画布尺寸的 PNG，内容已在正确位置，按 z_index 从小到大叠加即得原图\n"
        "source/         模型原始输出的图层 PNG（缩放后的实际尺寸）\n"
        "layers.json     图层元数据：name / z_index / description / bbox / size\n"
        "viewer.html     双击用浏览器打开，可勾选显隐，无需服务器\n\n"
        "关于坐标\n"
        "--------\n"
        "bbox.absolute 为 [left, top, right, bottom]，单位是底图像素，原点在左上角；\n"
        "bbox.normalized 为 [left, top, right, bottom]，按画布宽高换算到 0~1000 的整数。\n"
        "还原公式：x=left, y=top, w=right-left, h=bottom-top。\n"
    )


def compose_png(result: dict[str, Any], job_path: Path) -> bytes:
    canvas = _compose(result, job_path)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_zip(job_path: Path, result: dict[str, Any]) -> Path:
    """打包分层图片，返回 zip 路径（同一任务重复导出会覆盖）。"""
    zip_path = job_path / f"seedream-layers-{job_path.name}.zip"
    width = int((result.get("canvas") or {}).get("w") or 0)
    height = int((result.get("canvas") or {}).get("h") or 0)
    if not width or not height:
        raise ValueError("任务缺少画布尺寸")

    slim = _slim_layers(result)
    base_file = (result.get("base") or {}).get("file")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("composite.png", compose_png(result, job_path))

        base_image = Image.open(job_path / base_file) if base_file else None
        if base_image is not None:
            buffer = io.BytesIO()
            base_image.convert("RGBA").save(buffer, format="PNG", optimize=True)
            archive.writestr(f"layers/{BASE_LAYER_NAME}", buffer.getvalue())

        for layer in result.get("layers") or []:
            aligned = layer.get("aligned_file")
            if aligned:
                path = job_path / aligned
                if path.is_file():
                    archive.write(path, f"layers/{Path(aligned).name}")
            original = layer.get("file")
            if original:
                path = job_path / original
                if path.is_file():
                    archive.write(path, f"source/{Path(original).name}")

        archive.writestr(
            "layers.json", json.dumps(slim, ensure_ascii=False, indent=2)
        )
        archive.writestr("viewer.html", _viewer_html(result, slim))
        archive.writestr("README.txt", _readme(result))

    return zip_path


def build_psd(job_path: Path, result: dict[str, Any]) -> Path:
    """TODO(psd): 预留接口，当前不实现。

    接入时（参考已验证可行性）：
        pip install psd-tools
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
        psd = PSDImage.new("RGBA", (result["canvas"]["w"], result["canvas"]["h"]))
        for layer in sorted(result["layers"], key=lambda item: item["z_index"]):
            image = Image.open(job_path / layer["aligned_file"]).convert("RGBA")
            psd.append(PixelLayer.frompil(image, psd, layer["name"], top=0, left=0))
        psd.save(job_path / f"seedream-layers-{job_path.name}.psd")

    aligned_file 已经是整画布坐标，top/left 固定 0，不需要再做几何换算。
    """
    raise NotImplementedError("PSD 导出暂未实现，当前使用分层图片导出")
