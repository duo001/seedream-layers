# 运行架构与实现逻辑

这份文档回答三个问题：**请求怎么流动、数据长什么样、前端为什么那样写**。
目标是不用逐行读代码，也能知道某个功能该改哪里。

建议阅读顺序：先看第 1 节建立全局印象，再按你要改的功能跳到对应章节。
只想快速上手改代码，可以直接看最后一节「改哪里」。

---

## 1. 全局拓扑

```
┌──────────────────────────┐
│  浏览器（web/ 单页）      │
│  index.html / style.css  │
│  app.js                  │
│                          │
│  localStorage:           │
│    seedream.baseUrl      │
│    seedream.key          │
│    seedream.model        │
└────────────┬─────────────┘
             │  HTTP，仅 127.0.0.1
             ▼
┌──────────────────────────┐     HTTPS      ┌──────────────────────┐
│  本地 FastAPI (app.py)    │ ─────────────> │ 火山方舟 Agent Plan   │
│                          │                │ ark.cn-beijing        │
│  /api/decompose          │ <───────────── │ .volces.com/api/plan  │
│  /api/generate           │  data[].url    │                      │
│  /api/jobs               │    (24h)       └──────────────────────┘
│  /api/export/zip         │
│  /files/*  （静态）       │
│  /         （静态前端）    │
└────────────┬─────────────┘
             │ 下载图片、按 bbox 对齐、写 result.json
             ▼
      output/<job_id>/
```

三层职责很干净：

| 层 | 负责 | 不负责 |
|---|---|---|
| 浏览器 | 配置、预检、画布交互、前端合成 | 不直接调方舟，不接触落盘目录 |
| 本地 FastAPI | 校验、转发、下载图片、对齐、打包 | 不保存 Key、不做账号、不做金额换算 |
| 方舟 | 文生图、图层拆分 | 不保证图片 URL 长期有效（24h） |

### 三条贯穿全局的原则

1. **Key 只经过，不停留。**
   `localStorage` → 请求体 → FastAPI 内存 → `Authorization` 头 → 方舟。
   不写服务端文件、不打日志、不放 URL（所以 uvicorn 的访问日志里看不到 Key）。

2. **响应一到就落盘。**
   方舟返回的是临时 URL，`jobs.create_job()` 在返回响应前就把所有图片下载到
   `output/<job_id>/`，`result.json` 只记录本地相对路径。隔天再打开历史任务仍然可用。

3. **前端零构建。**
   没有 npm、没有打包器。`index.html` 直接 `<script src="app.js">`，
   改完刷新浏览器即可。后端也不会做模板渲染，`/` 只是静态目录。

---

## 2. 两条请求链路

入口都在 `web/app.js::run()`，根据 `state.mode` 二选一。

```
run()
 ├─ mode === 'upload' → runDecompose() → POST /api/decompose（multipart）
 └─ mode === 'text'   → runGenerate()  → POST /api/generate （JSON）
                                              │
                                              ▼
                                        applyJob(job)
```

### 2.1 上传拆分

```
浏览器                          app.py                       ark_client.py              jobs.py
  │                              │                              │                        │
  │ 预检（格式/大小/像素/比例）      │                              │                        │
  │ POST /api/decompose          │                              │                        │
  │ multipart: image + 配置       │                              │                        │
  ├─────────────────────────────>│                              │                        │
  │                              │ _check_request()             │                        │
  │                              │ _prepare_image()             │                        │
  │                              │  → data:image/png;base64,... │                        │
  │                              ├─────────────────────────────>│ decompose()            │
  │                              │                              │ POST /images/generations│
  │                              │                              │<───────────────────────│
  │                              │                              │ data[] + usage         │
  │                              │<─────────────────────────────┤                        │
  │                              ├─────────────────────────────────────────────────────>│
  │                              │                              │  create_job()          │
  │                              │                              │  · 并发下载所有图片      │
  │                              │                              │  · 写 base / layer_NN  │
  │                              │                              │  · 生成 canvas/ 对齐版  │
  │                              │                              │  · 写 result.json      │
  │                              │<─────────────────────────────────────────────────────┤
  │  result.json 内容            │                              │                        │
  │<─────────────────────────────┤                              │                        │
  │                              │                              │                        │
  │ GET /files/<job>/... 取图     │                              │                        │
  ├─────────────────────────────>│                              │                        │
```

关键点：

- **上传走 multipart，不是 JSON。** 30MB 图片转 base64 会膨胀到约 40MB；
  multipart 只传原始字节，由服务端再转 data URL 给方舟。
- **前端预检 + 后端再校验一次。** 前端 `setFile()` 先挡掉明显不合法的文件，
  避免白花一次计费；后端 `_prepare_image()` 不信任前端，仍完整校验。
- 校验规则：PNG / JPEG、≤30MB、总像素 `512×512 ~ 6000×6000`、宽高比 `1/16 ~ 16`。
- `prompt` 是可选的「拆分提示词」，不填就不放进上游请求体。

请求体（`ark_client.decompose()`）：

```json
{
  "model": "doubao-seedream-5-0-pro",
  "image": "data:image/png;base64,...",
  "layer_decomposition": true,
  "size": "auto",
  "output_format": "png",
  "response_format": "url",
  "watermark": false,
  "prompt": "可选",
  "layer_decomposition_options": { "max_images": 8 }
}
```

`max_images` 只在大于 0 时出现，并被夹到 `2 ~ 100`；
`text_recognition` 是预留参数，当前接口没有暴露。

### 2.2 文生图 + 拆分

这是**两次串行**的上游调用，共用同一份配置：

```
POST /api/generate
  │
  ├─ 1. ark_client.text_to_image()
  │      size = "2K" if 用户选 auto else 用户选的档位
  │      返回 data[0].url 或 b64_json
  │
  ├─ 2. ark_client.decompose()
  │      image = 上一步的 url / b64_json
  │      size  = 用户原始选择（auto 就传 auto）
  │
  └─ jobs.create_job(..., generate_usage=第一步的 usage)
```

两个容易忽略的细节：

- **`auto` 只影响第一步。** 文生图必须有一个具体尺寸，所以 `auto` 被替换成 `2K`；
  拆分那一步仍传 `auto`，让模型按输入图决定。
- **`generated_image` 是临时字段。** `app.py` 在 `create_job()` 之后才把上游 URL
  塞进返回值，它不会被写进 `result.json`（24 小时后必然失效）。
  前端目前不使用它。

### 2.3 返回什么

两个接口都返回同一份 `result` 字典（即 `result.json` 的内容）。
前端拿到后只做一件事：`applyJob(job)`，把服务端数据变成可编辑的会话状态。

---

## 3. 磁盘上的任务包

`output/` 被 `.gitignore` 排除，是纯运行时产物。

```
output/
└─ 20261118-171407-52f3/
   ├─ base.png                         底图（模型输出，通常是整画布）
   ├─ layer_01.png                     图层原图（模型返回的尺寸）
   ├─ layer_02.png
   ├─ canvas/
   │  ├─ 01-标题.png                    整画布对齐版，其余区域透明
   │  └─ 02-装饰元素.png
   ├─ result.json                      唯一元数据入口
   └─ seedream-layers-20261118-171407-52f3.zip   （导出后才有）
```

### 目录与命名规则

| 项目 | 规则 | 代码位置 |
|---|---|---|
| `job_id` | `YYYYMMDD-HHMMSS-xxxx`，后 4 位随机十六进制 | `jobs._new_job_id()` |
| 合法性 | `^\d{8}-\d{6}-[0-9a-f]{4}$`，防止路径穿越 | `jobs.JOB_ID_RE`、`jobs.job_dir()` |
| 图层原图 | `layer_<z_index:02d>.png` | `jobs.create_job()` |
| 对齐图 | `canvas/<z_index:02d>-<净化后的名称>.png` | 同上 |
| 文件名净化 | `\/:*?"<>\|` 和换行制表符替换为 `-`，去首尾空格/点/横线，最长 40 字 | `jobs._safe_name()` |
| 底图 | `base.<png/jpg/...>`，扩展名跟模型返回格式 | `jobs._ext_of()` |

中文名是允许的。前端访问时会对每一段路径做 `encodeURIComponent`
（`web/app.js::fileUrl()`），所以 `canvas/01-标题.png` 会变成
`/files/<job>/canvas/01-%E6%A0%87%E9%A2%98.png`，浏览器和 ZIP 都不会乱码。

---

## 4. 两套图层图片（最容易踩坑的地方）

每个图层在磁盘上有**两份**内容相同、但尺寸和用途完全不同的 PNG。

| | `layer_NN.png`（source） | `canvas/NN-名称.png`（aligned） |
|---|---|---|
| 尺寸 | 模型返回的图层尺寸，通常等于 bbox 的宽高 | 整画布 `canvas.w × canvas.h` |
| 内容位置 | 内容铺满图片，**位置信息在图片之外**（bbox） | 内容已贴到正确像素位置，其余透明 |
| 谁在用 | 网页画布、前端「下载合成图」 | ZIP 的 `layers/`、`viewer.html`、未来的 PSD |
| 优点 | 小、拖动缩放只影响这一块 | 消费方直接按顺序叠加即可，无需读 bbox |
| 代价 | 消费方必须自己按 bbox 摆位置 | 每层都是整画布大小，磁盘和内存更高 |

### 为什么网页用 source，而不是 aligned

网页的图层是一个 `div.layer-box`，几何由 `x / y / w / h` 控制，图片只是填充这个盒子：

```js
// web/app.js::createLayerElement()
img.src = fileUrl(layer.file);   // ← layer_NN.png，不是 aligned_file
```

如果改用 aligned：

1. 每个盒子都得是整画布大小，否则内容会被裁掉；
2. 拖动时移动的是一张带大量透明边距的整画布图，用户看到的位移和
   `x / y` 不再直观对应；
3. 缩放盒子时，透明边距和内容一起被拉伸，内容会相对 bbox 漂移；
4. N 个图层各一张全画布位图，内存直接乘以 N。

所以网页的坐标模型是：**source 图 = 内容，bbox = 位置**。两者分开存，
拖动只改位置，不重新采样图片。

### 为什么 ZIP 用 aligned

导出包要给 Photoshop、Figma、脚本或普通用户直接使用。消费方不应该被迫理解
`bbox.absolute` 和坐标原点，所以 `exporter.build_zip()` 直接打包对齐版：

```python
archive.write(path, f"layers/{Path(aligned).name}")
```

按 `z_index` 从小到大叠加 `layers/*.png` 就能得到原图。
`source/` 也一起放进去，方便需要原始裁切图的人。

### 对齐是怎么做的

`jobs._align_layer()`：

1. 新建一张 `canvas.w × canvas.h` 的全透明 RGBA 画布；
2. 如果图层图尺寸和 bbox 宽高不一致，先用 LANCZOS 缩放到 bbox 尺寸；
3. 把图贴到 `(left, top)`；
4. bbox 越界时做裁剪兜底，避免 `alpha_composite` 抛异常。

这里必须用 `alpha_composite`，不能用 `paste(..., mask=alpha)`。
后者会把 alpha 再乘一次，半透明图层的边缘会整体变暗。这是实际踩过的坑，
代码注释里也留了说明。

---

## 5. `result.json` 数据模型

这是前后端唯一的数据契约。`GET /api/jobs/{id}` 原样返回它。

```json
{
  "job_id": "20261118-171407-52f3",
  "created_at": "2026-11-18T09:14:08+00:00",
  "model": "doubao-seedream-5-0-pro",
  "request": {
    "mode": "upload",
    "size": "auto",
    "watermark": false,
    "max_images": null,
    "prompt": "",
    "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
    "source_size": "1200x800"
  },
  "usage": {
    "input_images": 1,
    "generated_images": 3,
    "output_tokens": 8900,
    "total_tokens": 8900
  },
  "usage_generate": {
    "generated_images": 1,
    "output_tokens": 2400,
    "total_tokens": 2400
  },
  "usage_total": {
    "input_images": 1,
    "generated_images": 4,
    "output_tokens": 11300,
    "total_tokens": 11300
  },
  "canvas": { "w": 1200, "h": 800 },
  "base": {
    "file": "base.png",
    "size": "1200x800",
    "output_format": "png"
  },
  "layers": [
    {
      "id": "l1",
      "z_index": 1,
      "name": "标题",
      "description": "顶部居中的大标题文字",
      "file": "layer_01.png",
      "aligned_file": "canvas/01-标题.png",
      "size": "200x140",
      "bbox": {
        "absolute": [120, 90, 320, 230],
        "normalized": [100, 112, 266, 287]
      },
      "text_recognition": null
    }
  ]
}
```

### 字段说明

| 字段 | 含义 | 注意 |
|---|---|---|
| `created_at` | UTC ISO 时间 | **不是本地时间**，前端历史列表直接截断显示 |
| `request.mode` | `upload` 或 `text` | `/api/jobs` 会带出 `mode` 字段 |
| `request.source_size` | 上传模式的原始尺寸 | 文生图模式没有这个字段 |
| `usage` | 拆分那次调用的原始 usage | 原样保存，不做加工 |
| `usage_generate` | 文生图那次的 usage | 仅 `text` 模式存在 |
| `usage_total` | 两次调用按字段相加 | 旧任务没有，读取时现场补算 |
| `canvas` | 底图像素尺寸 | 所有几何的基准坐标系 |
| `base.file` | 底图相对路径 | 历史缩略图也用它 |
| `layers[].file` | 图层原图 | 网页画布用 |
| `layers[].aligned_file` | 整画布对齐图 | 导出、预览页用 |
| `layers[].size` | 模型报告的图层尺寸 | 仅展示 |
| `layers[].bbox.absolute` | `[left, top, right, bottom]`，底图像素 | 前端几何的唯一来源 |
| `layers[].bbox.normalized` | 0~1000 的整数坐标 | 模型给的，原样保存，前端不用 |
| `layers[].text_recognition` | 文字识别结果 | 当前模型不支持，通常为 `null` |

### 坐标系统

只有一套真正参与计算的坐标：**底图像素空间**。

```
(0,0) ──────────────────> x
  │   ┌─────────────┐
  │   │  bbox       │
  │   │  (l,t,r,b)  │
  │   └─────────────┘
  ▼
  y                 canvas.w × canvas.h
```

- `x = left`，`y = top`，`w = right - left`，`h = bottom - top`
- 宽高至少为 1，避免除以 0 或生成空盒子
- `normalized` 只是给外部工具的元数据，前端从不拿它做换算

为什么不用 0~1000 归一化：模型给的归一化值是整数，换算回 6000px 画布时
一个单位就是 6px，取整误差会直接表现为图层错位。绝对像素是精确值。

### 两个时间字段的差异

- `job_id` 用**本地时间**生成（`datetime.now()`），方便肉眼对照；
- `created_at` 用 **UTC**（`datetime.now(timezone.utc)`）。

历史列表把 `created_at` 直接 `replace('T', ' ').slice(0, 19)` 显示，
没有做时区转换，所以东八区用户会看到比 `job_id` 早 8 小时的时间。
这是展示层的既有行为，不是数据错误。

---

## 6. 后端模块职责

| 文件 | 职责 | 关键函数 |
|---|---|---|
| `app.py` | HTTP 路由、上传预检、静态挂载、错误映射 | `api_decompose`、`api_generate`、`_prepare_image`、`_check_request` |
| `ark_client.py` | 拼方舟请求、发请求、翻译错误、Base URL 白名单 | `decompose`、`text_to_image`、`_post`、`validate_base_url` |
| `jobs.py` | 下载图片、落盘、整画布对齐、读写 `result.json` | `create_job`、`_align_layer`、`sum_usage`、`list_jobs` |
| `exporter.py` | ZIP、合成图、离线预览页、PSD 预留 | `build_zip`、`compose_png`、`_viewer_html` |
| `web/` | 单页前端 | `app.js::applyJob` 等 |

### 路由与静态挂载顺序

```python
app.mount("/files", StaticFiles(directory=jobs.OUTPUT_DIR), name="files")
# ... 所有 /api 路由 ...
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
```

`/files` 在前，`/` 在最后。Starlette 按注册顺序匹配，所以：

- `/api/*` 先命中具体路由；
- `/files/<job>/...` 命中输出目录；
- 其余路径落到 `web/`，`/` 自动返回 `index.html`。

如果把 `/` 提前挂载，所有 API 都会被静态目录吃掉。

### 上传预检（两层）

| 检查 | 前端 `setFile()` | 后端 `_prepare_image()` |
|---|---|---|
| MIME / 格式 | `image/png`、`image/jpeg` | 实际解码后看 `image.format` |
| 大小 | ≤30MB | ≤30MB |
| 总像素 | `512×512 ~ 6000×6000` | 同左 |
| 宽高比 | `1/16 ~ 16` | 同左 |

前端用 `createImageBitmap()` 读尺寸，失败会 toast；后端用 Pillow `load()` 完整解码，
损坏文件会返回 400。两层的错误文案都直接给用户看，不需要再翻译。

### 错误映射

```
ark_client.ArkError
  ├─ HTTP >= 400  → message / status / 原始 JSON
  ├─ 超时          → "请求方舟超时（300s）..."
  └─ 网络异常      → "请求方舟失败：..."
        │
        ▼  app.py::_upstream_error()
HTTP 502
{
  "detail": {
    "message": "可读的中文说明",
    "upstream_status": 401,
    "upstream": { ...方舟原始报文... }
  }
}
```

前端 `messageOf()` 会优先取 `detail.message`，并在有 `upstream_status` 时追加
`（上游 HTTP 401）`。原始报文只 `console.warn`，不弹给普通用户。

其他状态码：

| 场景 | 状态码 |
|---|---|
| 参数 / 图片不合法 | 400 |
| 任务 ID 不存在或格式不合法 | 404 |
| 方舟报错、超时、网络失败 | 502 |
| 打包失败 | 500 |

上传链路会把 `create_job()` 的 `ValueError`（例如上游没返回任何图层）转成 502；
文生图链路只显式校验了第一步是否拿到图片，第二步的空数据会落到 500。这是两条
链路的一处不对称，改错误处理时注意。

### Base URL 白名单（SSRF 防线）

`validate_base_url()` 只允许：

- `*.volces.com`
- `localhost`
- `127.0.0.1`

可通过环境变量扩展：

```bash
ARK_ALLOWED_HOSTS=volces.com,localhost,127.0.0.1,my-proxy.local
```

它校验的是**主机名**，不限制路径。所以本地服务仍然可以把请求发到白名单主机的
任意路径，这是为了兼容方舟不同区域的 endpoint。不要把这个服务暴露到公网。

---

## 7. 前端状态与坐标

### 7.1 唯一真相：`state`

`web/app.js` 没有框架，所有可变数据集中在一个对象里：

```js
const state = {
  mode: 'upload',          // upload | text
  file: null,              // 待上传的 File
  previewUrl: null,        // 上传预览的 objectURL
  jobId: null,             // 当前任务
  job: null,               // 服务端原始 result
  usage: {},               // 汇总后的用量
  canvas: { w: 0, h: 0 },  // 底图像素尺寸
  base: null,              // { file, visible, size, el }
  layers: [],              // 可编辑图层数组
  selected: null,          // 当前选中图层
  zoom: 1,                 // 画布缩放
  busy: false,
  controller: null,        // AbortController
  timerId: null,
  startedAt: 0,
  config: { baseUrl, key, model },
  defaults: { ... },
  models: [],
};
```

渲染函数只读 `state`，事件处理只改 `state` 再调用对应的渲染函数。
没有虚拟 DOM，也没有双向绑定。

### 7.2 从服务端结果到可编辑图层

`applyJob(job)` 是唯一的入口，做五件事：

```js
function applyJob(job) {
  // 1. 记录任务与画布
  state.job = job;
  state.jobId = job.job_id;
  state.canvas = { w: job.canvas.w, h: job.canvas.h };
  state.base = { file: job.base.file, visible: true, el: null, size: job.base.size };

  // 2. bbox → 盒子几何
  state.layers = job.layers.map((layer) => {
    const bbox = layer.bbox?.absolute || null;
    const width  = bbox ? bbox[2] - bbox[0] : state.canvas.w;
    const height = bbox ? bbox[3] - bbox[1] : state.canvas.h;
    const item = {
      id, name, description,
      alignedFile: layer.aligned_file,  // 保留但页面不用
      file: layer.file,                 // ← 页面渲染用这张
      size,
      x: bbox ? bbox[0] : 0,
      y: bbox ? bbox[1] : 0,
      w: Math.max(1, width),
      h: Math.max(1, height),
      opacity: 1,
      visible: true,
      order: layer.z_index || index + 1,
      el: null,
    };
    item.origin = { x: item.x, y: item.y, w: item.w, h: item.h }; // 用于「还原」
    return item;
  }).sort((a, b) => a.order - b.order);

  // 3. 规整层级、清空选择
  normalizeOrders();
  state.selected = null;

  // 4. 显示工具栏 / 图层面板，写用量胶囊
  // 5. 渲染
  renderBoard();
  renderLayerList();
  fitZoom();
}
```

会话中的图层对象比 `result.json` 多出这些字段：

| 字段 | 作用 |
|---|---|
| `x / y / w / h` | 当前几何，初始来自 bbox |
| `origin` | 初始几何快照，双击或「重置位置」时还原 |
| `opacity` | 0~1，默认 1 |
| `visible` | 显隐 |
| `order` | 显示层级，规整为 1~N |
| `el` | 对应的 DOM 节点，渲染后才有 |

### 7.3 画布与缩放：两层尺寸

DOM 结构：

```
.canvas-area        （可滚动容器）
  └─ .sizer         （尺寸 = canvas × zoom，撑出正确的滚动条）
       └─ .board    （尺寸 = canvas 像素，transform: scale(zoom)）
            ├─ img.base-img      宽高 100%
            └─ div.layer-box     绝对定位，left/top/width/height 为像素
```

关键点：

- `.board` 的布局尺寸永远是 `canvas.w × canvas.h`，缩放只加
  `transform: scale(zoom)`，`transform-origin: 0 0`。
- `.sizer` 的宽高是 `canvas × zoom`，否则 `transform` 不改变布局，
  滚动区域会按未缩放尺寸计算，出现滚动条范围不对。
- 图层盒子的 `left / top / width / height` 都是**画布像素**，不受缩放影响。
- `applyZoom()` 同时更新 `state.zoom`、`.board` 的 transform、`.sizer` 的尺寸和百分比标签。

### 7.4 指针坐标换算

因为 `transform` 不改变布局坐标，屏幕上的 1px 不等于画布里的 1px：

```js
// 拖动
layer.x = Math.round(startX + (event.clientX - startClientX) / state.zoom);
layer.y = Math.round(startY + (event.clientY - startClientY) / state.zoom);

// 缩放（等比）
const dx = (event.clientX - startClientX) / state.zoom;
const dy = (event.clientY - startClientY) / state.zoom;
const delta = Math.max(dx, dy);
layer.w = Math.max(24, startW + delta);
layer.h = Math.round(layer.w / aspect);
```

所有屏幕位移都要除以 `state.zoom`。方向键微调不需要，因为它直接改画布像素
（1px 或 `Shift` 的 10px）。

缩放范围：手动 `0.05 ~ 8`，适应窗口时不超过 1、不小于 `0.03`
（`fitZoom()` 会预留 56px 边距）。

### 7.5 渲染顺序与层级

- `renderBoard()`：先清空 `.board`，画底图，再按 `state.layers` 顺序追加图层盒子。
- 盒子的 `z-index` 用 `layer.order`；底图不带 z-index，天然在最下面。
- 右侧图层列表**倒序**显示（顶层在上），底图固定第一项。
- `moveLayer()` 在排序后的数组里交换相邻元素，然后把 `order` 重排为 1~N；
  列表和 DOM 的 z-index 同步更新。
- `normalizeOrders()` 在载入、删除后调用，保证层级连续，避免出现 0 或重复值。

### 7.6 哪些操作只影响会话

前端**从不写回 `result.json`**。以下操作只改内存：

| 操作 | 影响 |
|---|---|
| 拖动、缩放、方向键 | `x / y / w / h` |
| 透明度滑块 | `opacity` |
| 显隐、全部显隐 | `visible`、`state.base.visible` |
| 上移 / 下移 | `order` |
| 删除图层 | 从 `state.layers` 移除 |
| 改名 | `name` |
| 双击 / 重置 | 回到 `origin` |

刷新页面即恢复模型原始布局。重新从历史任务载入，也是读磁盘上的原始
`result.json`。这是刻意设计：导出包必须保持模型布局，手动微调只服务于
「下载合成图」。

### 7.7 配置的存取

- 键名：`seedream.baseUrl` / `seedream.key` / `seedream.model`
- 旧键名 `img2psd.*` 在第一次读取时自动迁移并删除（`readSetting()`）
- 保存时写入 `localStorage`，同时清掉旧键
- 「恢复默认」保留 Key，只重置 Base URL 和 Model ID
- 「清除 Key」只删 `seedream.key`
- 启动时 `GET /api/health` 拉默认值和候选模型，失败只提示，不阻塞页面

---

## 8. 导出链路

有两条完全独立的导出路径，用途不同。

### 8.1 下载合成图（前端，所见即所得）

`web/app.js::exportComposite()`：

```
新建 <canvas>，尺寸 = canvas.w × canvas.h（不是缩放后的尺寸）
  ├─ 底图可见 → drawImage(base, 0, 0, w, h)
  └─ 按 order 升序遍历可见图层
       context.globalAlpha = layer.opacity
       drawImage(layer.file, layer.x, layer.y, layer.w, layer.h)
  → canvas.toBlob('image/png') → 下载
```

特点：

- 使用**会话状态**，拖动、缩放、透明度、显隐都会体现；
- 用 `layer.file`（source）+ 当前几何，而不是 aligned；
- 画布初始透明，底图被隐藏时导出的是透明背景 PNG；
- 图片通过 `/files/` 加载，需要本地服务在运行。

### 8.2 导出分层包（后端，模型原始布局）

`GET /api/export/zip?job=<id>` → `exporter.build_zip()`：

```
composite.png            白底 + 底图 + 所有 aligned 图层，按 z_index 叠加
layers/00-背景.png        底图转 RGBA
layers/NN-名称.png        aligned_file 的副本
source/layer_NN.png       layer_NN.png 的副本
layers.json               每层的 name / z_index / description / file /
                          source / size / bbox；顶层还有 usage 字段
viewer.html               单文件预览页
README.txt                坐标还原说明 + 用量
```

特点：

- 使用**磁盘上的 aligned 图**，与前端会话状态无关；
- ZIP 写到任务目录，同一任务重复导出会覆盖；
- `FileResponse` 以 `seedream-layers-<job_id>.zip` 下载；
- 中文文件名在 ZIP 里是 UTF-8，`viewer.html` 用相对路径，解压后双击即可离线打开。

### 8.3 为什么两条路径结果可能不一样

| | 前端合成 PNG | 后端 ZIP |
|---|---|---|
| 数据源 | 当前会话 + source 图 | 磁盘 + aligned 图 |
| 反映手动调整 | 是 | 否 |
| 背景 | 跟随底图显隐（可透明） | 固定白底 |
| 用途 | 快速交付一张图 | 给设计工具继续加工 |

这不是 bug。README 和使用说明都写明了：分层包永远是模型原始布局。

### 8.4 PSD

`exporter.build_psd()` 当前直接抛 `NotImplementedError`。
注释里留了 `psd-tools` 的实现要点：aligned 图已经是整画布坐标，
`PixelLayer.frompil(..., top=0, left=0)` 即可，不需要再做几何换算。

---

## 9. 用量统计

### 9.1 数据来源

| 字段 | 来源 | 何时存在 |
|---|---|---|
| `usage` | 拆分调用的 `response.usage` | 总是 |
| `usage_generate` | 文生图调用的 `usage` | 仅文生图模式 |
| `usage_total` | `sum_usage()` 按字段相加 | 新任务总是 |

`sum_usage()` 只累加四个数值字段：
`input_images`、`generated_images`、`output_tokens`、`total_tokens`，
忽略布尔值和非数字，避免上游字段类型变化把汇总搞坏。

### 9.2 旧任务兼容

早期任务没有 `usage_generate` / `usage_total`。读取时：

- 后端 `list_jobs()`：`result.get("usage_total") or sum_usage(result)`
- 前端 `sumUsage(job)`：没有 `usage_total` 就现场遍历 `usage_generate` + `usage`
- 导出 `_slim_layers()`：缺字段时写空对象，不报错

所以旧任务的历史列表、重新载入、导出包都不会缺用量。

### 9.3 展示口径

- 工具条胶囊：`4 张 · 11,300 tk`
- 悬停：分阶段列出「文生图 / 拆分层 / 合计」
- 历史每项：`模型 · 4 张 · 11,300 tk`
- 抽屉底部：最近 N 次的张数与 token 累计

`output_tokens` 是 `Σ(宽×高)/256` 的面积折算值，**不参与计费**。
真实账单看 `generated_images` 张数，以方舟控制台为准。
项目刻意不做金额换算，避免给出错误的费用预期。

---

## 10. 安全边界

这是本机自用工具，安全模型很简单，但必须清楚：

| 项 | 现状 | 含义 |
|---|---|---|
| 监听地址 | `127.0.0.1:8787` | 默认只有本机可访问 |
| `/api/*` 鉴权 | 无 | 任何能访问端口的人都能调用 |
| `/files/*` 鉴权 | 无 | 能读取 `output/` 下任意已知路径的文件 |
| Key 存储 | 浏览器 `localStorage`（明文） | 能读该浏览器 profile 的代码就能读到 |
| Key 传输 | 请求体 / 表单，不走 URL | uvicorn 默认访问日志不记录 body |
| Key 落盘 | 不落盘 | 服务端只在请求期间持有 |
| Base URL | 主机名白名单 | 降低被当作任意请求跳板的风险 |
| 任务 ID | 严格正则 + 目录存在性检查 | 防止 `/api/jobs`、`/api/export/zip` 的 job 参数用 `../` 穿越目录 |

因此：

- **不要绑定 `0.0.0.0` 后暴露到公网或公司内网。**
- 不要在别人的在线 demo 里填自己的 Key。
- 公共电脑用完点「清除 Key」。
- 换端口（`8787` → `8788`）后 `localStorage` 按 origin 隔离，需要重填配置。

---

## 11. 扩展点

### 11.1 接入 PSD 导出

`exporter.build_psd()` 已留好位置：

```python
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer

psd = PSDImage.new("RGBA", (result["canvas"]["w"], result["canvas"]["h"]))
for layer in sorted(result["layers"], key=lambda item: item["z_index"]):
    image = Image.open(job_path / layer["aligned_file"]).convert("RGBA")
    psd.append(PixelLayer.frompil(image, psd, layer["name"], top=0, left=0))
psd.save(job_path / f"seedream-layers-{job_path.name}.psd")
```

还需要：

1. `app.py` 增加 `GET /api/export/psd`；
2. `app.py::health()` 的 `has_psd_export` 改成 `True`（前端可据此显示按钮）；
3. `requirements.txt` 加 `psd-tools`。

因为用的是 aligned 图，`top/left` 固定 0，前端不需要改任何几何逻辑。

### 11.2 打开文字识别

`ark_client.decompose()` 已经支持 `text_recognition=True`，但接口层没暴露：

1. `app.py::api_decompose` 加 `textRecognition: bool = Form(False)`；
2. `GenerateBody` 加同名字段；
3. 传给 `ark_client.decompose(..., text_recognition=...)`；
4. 前端加一个开关，放进 `commonForm()` / `runGenerate()` 的请求体；
5. `result.json` 里每个图层的 `text_recognition` 已经有位置，无需改结构。

注意：`doubao-seedream-5-0-pro` / `5.0-flash` 不支持这个参数，
开了也不会返回文字结构化结果，具体以套餐模型为准。

### 11.3 `max_images`

- 前端 `#maxImages` 输入 0~100，0 表示不传；
- `commonForm()` 只在 `> 0` 时放进表单；
- `ark_client.decompose()` 夹到 `2 ~ 100`（传 1 会变成 2）；
- 仅 Seedream 5.5 生效，其他模型传了也会被忽略。

如果要做「按模型显示/隐藏」，在 `web/app.js::syncConfigUI()` 里根据
`state.config.model` 切换输入框的 `disabled` 即可。

### 11.4 其他可扩展方向

| 需求 | 改动点 |
|---|---|
| 保存手动调整 | 新增 `PATCH /api/jobs/{id}` 写 `edits.json`，`applyJob` 时优先读它 |
| 限制并发 | `app.py` 加 `asyncio.Semaphore`，包住两次上游调用 |
| 局域网加鉴权 | 启动时生成一次性 token，前端存内存，所有 `/api` 和 `/files` 校验 |
| 模型列表动态化 | `/api/health` 已经返回 `models`，可改成从配置或上游拉取 |
| 多图批量拆分 | 新增批量接口 + 任务队列，`jobs.create_job` 本身已是单任务纯函数 |

---

## 12. 改哪里：按需求定位

| 想改的东西 | 先看 |
|---|---|
| 上传限制、格式、错误文案 | `app.py::_prepare_image` + `web/app.js::setFile` |
| 方舟请求体、超时、错误翻译 | `ark_client.py::decompose` / `text_to_image` / `_post` |
| Base URL 白名单 | `ark_client.py::validate_base_url` + `ARK_ALLOWED_HOSTS` |
| 落盘结构、文件命名、bbox 对齐 | `jobs.py::create_job` / `_align_layer` / `_safe_name` |
| `result.json` 字段 | `jobs.py::create_job` + 本文第 5 节 |
| 历史列表、缩略图、排序 | `jobs.py::list_jobs` + `web/app.js::loadHistory` |
| ZIP 内容、`layers.json`、预览页 | `exporter.py::build_zip` / `_slim_layers` / `_viewer_html` |
| 合成图算法 | `web/app.js::exportComposite`（前端）/ `exporter._compose`（后端） |
| 画布几何、拖拽、缩放、层级 | `web/app.js::applyJob` / `renderBoard` / `onLayerPointerDown` / `onResizePointerDown` |
| 用量展示 | `web/app.js::sumUsage` / `usageLabel` / `usageDetail` |
| 样式、窄屏布局 | `web/style.css` |
| PSD / 文字识别 / 新参数 | 本文第 11 节 |

### 推荐阅读顺序

1. `web/index.html`：认清所有元素 ID 和页面分区；
2. `web/app.js::boot` / `bindEvents`：知道事件怎么绑定；
3. `web/app.js::run` → `runDecompose` / `runGenerate`：知道请求怎么发；
4. `app.py::api_decompose` → `ark_client.decompose` → `jobs.create_job`：跟一次完整落盘；
5. `web/app.js::applyJob` → `renderBoard` → 拖拽/缩放函数：理解画布状态机；
6. `exporter.build_zip`：理解导出包和 aligned 图的关系。

---

## 13. 调试与测试

### 假方舟（不花额度）

```bash
python tests/fake_ark.py     # 127.0.0.1:8799，固定返回 960×640 + 2 图层
```

页面配置：

```
Base URL : http://127.0.0.1:8799/api/plan/v3
API Key  : fake
Model ID : doubao-seedream-fake
```

`fake_ark.py` 同时模拟了文生图和拆层两个接口，可以完整跑通两种模式。
之所以能连本地地址，是因为白名单默认包含 `127.0.0.1`。

### 离线冒烟

```bash
python tests/smoke_local.py
```

覆盖：

- `create_job` 的落盘、bbox 对齐、文件名净化；
- `usage` / `usage_generate` / `usage_total` 合并；
- `list_jobs` 的用量回退；
- ZIP 内容、`layers.json`、`viewer.html`、`README.txt`；
- `build_psd` 仍抛 `NotImplementedError`。

### 提交前检查

```bash
node --check web/app.js
python -m py_compile app.py ark_client.py jobs.py exporter.py tests/smoke_local.py
python tests/smoke_local.py
```

`node --check` 只做语法检查，不执行代码；前端没有构建步骤，这是最轻的回归手段。
需要看真实交互时，可以用 Playwright 起 `fake_ark.py` + uvicorn，
在 `page.evaluate` 里写入 `localStorage` 的 `seedream.*` 三项，再走真实页面操作。

### 常见现象对照

| 现象 | 先查 |
|---|---|
| 页面能开但请求全 404 | `/` 静态挂载是否被提前注册 |
| 图片 404 | `result.json` 里的相对路径、文件名是否被净化、URL 是否编码 |
| 图层位置整体偏移 | bbox 是否被当成了 `[left, top, w, h]`（实际是 `[l, t, r, b]`） |
| 半透明边缘发暗 | 是否误用了 `paste(mask=alpha)` |
| 缩放到 50% 后拖动距离不对 | 是否漏除 `state.zoom` |
| 历史时间比任务 ID 早 8 小时 | `created_at` 是 UTC，属正常 |
| ZIP 里位置和网页不一致 | 正常：ZIP 用模型原始布局，网页可能被手动改过 |
