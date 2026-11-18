"""本地假方舟：不需要 Key、不消耗额度，用来调 UI 和导出逻辑。

运行：
    python tests/fake_ark.py            # 默认 127.0.0.1:8799

然后在页面「配置」里填：
    Base URL : http://127.0.0.1:8799/api/plan/v3
    API Key  : 随便填（例如 fake）
    Model ID : 随便填（例如 doubao-seedream-fake）

它会同时模拟文生图接口和图层拆分接口。
"""

from __future__ import annotations

import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

PORT = 8799


def _data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _base_image() -> Image.Image:
    image = Image.new("RGB", (960, 640), (26, 30, 42))
    draw = ImageDraw.Draw(image)
    for y in range(640):
        draw.line([(0, y), (960, y)], fill=(26 + y // 14, 30 + y // 20, 58))
    draw.rectangle([90, 70, 870, 210], outline=(96, 146, 255), width=3)
    draw.ellipse([520, 330, 800, 580], fill=(226, 126, 92))
    return image


def _layer(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([2, 2, size[0] - 2, size[1] - 2], radius=16, fill=color)
    return layer


def image_response(prompt: str) -> dict:
    return {
        "model": "doubao-seedream-fake",
        "created": 0,
        "data": [{"url": _data_url(_base_image()), "size": "960x640", "output_format": "png"}],
        "usage": {"generated_images": 1, "output_tokens": 2400, "total_tokens": 2400},
    }


def layer_response(prompt: str) -> dict:
    return {
        "model": "doubao-seedream-fake",
        "created": 0,
        "data": [
            {"url": _data_url(_base_image()), "size": "960x640", "output_format": "png", "z_index": 0},
            {
                "url": _data_url(_layer((760, 130), (96, 146, 255, 225))),
                "size": "760x130",
                "output_format": "png",
                "z_index": 1,
                "bounding_box": {
                    "absolute": [100, 80, 860, 210],
                    "normalized": [104, 125, 896, 328],
                },
                "name": "标题文字",
                "description": "浅蓝色圆角标题条",
            },
            {
                "url": _data_url(_layer((300, 270), (226, 126, 92, 210))),
                "size": "300x270",
                "output_format": "png",
                "z_index": 2,
                "bounding_box": {
                    "absolute": [510, 320, 810, 590],
                    "normalized": [531, 500, 844, 922],
                },
                "name": "装饰元素",
                "description": "橙色椭圆色块",
            },
        ],
        "usage": {"input_images": 1, "generated_images": 3, "output_tokens": 8900, "total_tokens": 8900},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}

        prompt = payload.get("prompt") or ""
        if payload.get("layer_decomposition"):
            body = layer_response(prompt)
        else:
            body = image_response(prompt)

        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # 静音
        pass


if __name__ == "__main__":
    print(f"假方舟已启动：http://127.0.0.1:{PORT}/api/plan/v3")
    print("请求会返回 960×640 的底图 + 2 个图层，不消耗任何额度。")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
