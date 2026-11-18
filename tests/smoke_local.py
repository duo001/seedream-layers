"""离线冒烟测试：不调方舟、不需要 Key，验证落盘 / ZIP / 预览页整条链路。

运行：
    python tests/smoke_local.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import exporter  # noqa: E402
import jobs  # noqa: E402


def data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def make_base() -> Image.Image:
    image = Image.new("RGB", (800, 600), (28, 32, 44))
    draw = ImageDraw.Draw(image)
    for y in range(600):
        draw.line([(0, y), (800, y)], fill=(28 + y // 12, 32 + y // 18, 60))
    draw.rectangle([100, 60, 700, 180], outline=(90, 140, 255), width=3)
    draw.ellipse([420, 320, 640, 520], fill=(220, 120, 90))
    return image


def make_layer(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([2, 2, size[0] - 2, size[1] - 2], radius=14, fill=color)
    return layer


def fake_response() -> dict:
    return {
        "model": "doubao-seedream-fake",
        "data": [
            {"url": data_url(make_base()), "size": "800x600", "output_format": "png", "z_index": 0},
            {
                "url": data_url(make_layer((200, 140), (90, 140, 255, 220))),
                "size": "200x140",
                "output_format": "png",
                "z_index": 1,
                "bounding_box": {"absolute": [120, 90, 320, 230], "normalized": [150, 150, 400, 383]},
                "name": "标题/文字: 一层",
                "description": "深色粗体标题，带阴影",
            },
            {
                "url": data_url(make_layer((300, 260), (230, 120, 90, 200))),
                "size": "300x260",
                "output_format": "png",
                "z_index": 2,
                "bounding_box": {"absolute": [400, 300, 700, 560], "normalized": [500, 500, 875, 933]},
                "name": "装饰元素",
                "description": "橙色椭圆色块",
            },
        ],
        "usage": {"input_images": 1, "generated_images": 3, "output_tokens": 12345},
    }


def main() -> int:
    result = __import__("asyncio").run(
        jobs.create_job(
            fake_response(),
            model="doubao-seedream-fake",
            request_meta={"mode": "upload", "size": "auto"},
        )
    )
    job_dir = jobs.OUTPUT_DIR / result["job_id"]

    try:
        assert result["canvas"] == {"w": 800, "h": 600}, result["canvas"]
        assert len(result["layers"]) == 2, result["layers"]
        assert [layer["z_index"] for layer in result["layers"]] == [1, 2]

        for layer in result["layers"]:
            for key in ("file", "aligned_file"):
                assert (job_dir / layer[key]).is_file(), f"缺少 {layer[key]}"
            with Image.open(job_dir / layer["aligned_file"]) as aligned:
                assert aligned.size == (800, 600), aligned.size
            assert "/" not in layer["aligned_file"].split("/")[-1], "文件名未净化"
            assert ":" not in layer["aligned_file"].split("/")[-1], "文件名未净化"

        zip_path = exporter.build_zip(job_dir, result)
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            required = {"composite.png", "viewer.html", "layers.json", "README.txt", "layers/00-背景.png"}
            assert required <= names, required - names
            assert any(name.startswith("layers/0") and name.endswith(".png") for name in names)
            assert any(name.startswith("source/layer_") for name in names)

            viewer = archive.read("viewer.html").decode("utf-8")
            assert "图层列表" in viewer and "装饰元素" in viewer
            assert "</script>" in viewer

            meta = json.loads(archive.read("layers.json").decode("utf-8"))
            assert len(meta["layers"]) == 2
            assert meta["layers"][0]["bbox"]["absolute"] == [120, 90, 320, 230]

            composite = Image.open(io.BytesIO(archive.read("composite.png")))
            assert composite.size == (800, 600)

        loaded = jobs.load_job(result["job_id"])
        assert loaded["job_id"] == result["job_id"]
        assert any(item["job_id"] == result["job_id"] for item in jobs.list_jobs())

        try:
            exporter.build_psd(job_dir, result)
        except NotImplementedError:
            pass
        else:
            raise AssertionError("build_psd 应当仍未实现")

        print(f"OK  任务 {result['job_id']}：2 层 + 底图，ZIP 条目 {len(names)} 个")
        print(f"    zip: {zip_path.name} ({zip_path.stat().st_size / 1024:.0f}KB)")
        return 0
    finally:
        import shutil

        shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
