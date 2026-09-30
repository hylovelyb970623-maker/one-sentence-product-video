# 一句话生成带货视频（AI Director）

上传一张商品图、写一句真实介绍，点一次按钮，几分钟后拿到一条 **10 秒带货成片**（MP4，含中文字幕、价格贴片、原创背景音乐、AI 显式标识）。

面向电商卖家：你只负责商品素材，中间的编导策划、镜头提示词、视频生成、剪辑合成全部自动完成。技术细节（ComfyUI 工作流、提示词、分镜、任务 ID）只出现在管理后台，不进入用户界面。

## 它能做什么

- **一句话成片**：商品图（必需）＋ 商品名称与真实介绍（≤160 字）＋ 可选展示价格 → 一键生成 10 秒成片
- **竖版 / 横版**：9:16（抖音、快手、小红书）与 16:9（淘宝主图、B 站、视频号）
- **标准 / 高清**：432×768 / 720×1280（及对应横版），预计约 10 分钟 / 20–30 分钟
- **出镜人物形象**：可选上传一张已获肖像授权的人物照片，出镜主人公形象与其一致；成片完成后还可「一键换人物形象重做」——同一商品、同一切设置，只换人重拍
- **AI 编导链路**：视觉模型先「看」商品图识别品类形态 → 资深编导 LLM 按「困境 → 商品介入 → 状态转变」叙事弧写两镜脚本 → 转换成视频模型可执行的镜头提示词
- **卖点证据链**：写了卖点逐条转成可拍摄画面；没写则从图片可见特征推断 2–3 个「看得见的卖点」并标注「推断」
- **质检门**：自动抽帧检查商品是否过早出现、是否出现多余杯子（幽灵道具）、饮品类是否真实饮用（防「假喝」）、液面/液体量是否可见减少；不合格自动换种子重拍
- **智能体接口**：HTTP API 供 Coze / Dify / GPTs 等平台调用（Bearer 鉴权，dataURL 传图）
- **任务系统**：原子持久化、GPU 忙自动排队、网络抖动重试、重启不重复提交远端任务、中断任务需管理员确认后恢复
- **合规设计**：AI 显式标识写入画面与元数据；拦截未经审核的功效与绝对化宣称；字幕禁止编造数字；人物照必须勾选肖像授权

## 整体链路

```
商品图 + 一句话
   │
   ├─ S1 视觉识别（VL 模型）：客观描述商品形态
   ├─ S2 编导策划（LLM）：钩子 / 卖点证据链 / 两镜脚本
   ├─ S3 镜头提示词（MiniMax H3 可执行格式）
   ├─ S5/S6 双镜头生成（ComfyUI · Ref2VA）
   │      镜0：商品参考图锁外观
   │      镜1：商品参考图 + 上一镜收尾参考视频（人物/场景衔接）
   ├─ S7 质检门：抽帧视觉检查，不合格自动重拍
   └─ S8 FFmpeg 合成：字幕 / 价格贴片 / 原创 BGM / AI 标识 → 10s MP4
```

## 快速开始（macOS / Linux）

### 前置要求

- Python 3.11+
- 远端 ComfyUI 已部署 MiniMax H3（Ref2VA）工作流
- 一个文本 LLM API（编导策划，OpenAI 兼容协议，默认 DeepSeek）
- 一个视觉模型 API（看图识别＋质检，OpenAI 兼容协议，需支持图片输入）
- FFmpeg（可用 `FFMPEG_BINARY` 指定，否则自动使用 requirements 里的 `imageio-ffmpeg`）
- 中文字体（Mac 自带；Linux 需安装 Noto Sans CJK）

### 安装

```bash
git clone https://github.com/hylovelyb970623-maker/one-sentence-product-video.git
cd one-sentence-product-video/m0
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入你自己的配置
```

### 配置

编辑 `m0/.env`：

```ini
# 编导 LLM（OpenAI 兼容）
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# 视觉模型（看图识别 + 质检门，需支持图片输入）
VISION_API_KEY=sk-xxx
VISION_BASE_URL=https://your-openai-compatible-endpoint/v1
VISION_MODEL=your-vision-model

# 远端 ComfyUI（已部署 MiniMax H3 Ref2VA）
COMFYUI_HOST=http://your-comfyui:8188
COMFYUI_AUTH_USER=
COMFYUI_AUTH_PASS=
COMFYUI_WORKFLOW_DIR=./workflows

# 视频生成引擎：comfyui（默认）或 h3api（MiniMax 官方 API）
# VIDEO_ENGINE=h3api
# H3_API_KEY=sk-xxx
# H3_API_BASE_URL=https://api.minimaxi.com/v1
# H3_API_MODEL=MiniMax-H3

# 管理后台密码（≥16 字符；不配置则自动生成到 m0/.private/admin-password）
# DIRECTOR_ADMIN_PASSWORD=
# 智能体接口 Bearer Token（不配置则复用管理员密码）
# DIRECTOR_AGENT_TOKEN=
```

完整可填项见 [m0/.env.example](m0/.env.example)。

### 双引擎（自部署 ComfyUI / 官方 API）

生成引擎二选一，`VIDEO_ENGINE` 切换，两引擎对外接口完全一致（提交→任务 ID→轮询→下载），任务系统、质检门、剪辑链路零改动：

- `comfyui`（默认）：走你自部署的 ComfyUI + MiniMax H3 Ref2VA 工作流，零边际成本，适合有 GPU 的自用/保底链路
- `h3api`：走 MiniMax 官方 API，免 GPU 运维、天然弹性并发，适合对外放量；需配置 `H3_API_KEY`

两引擎可用同一份创意脚本 A/B 对比画质与一致性后再决定默认引擎。API 字段映射集中在 `pipeline/h3_api.py` 的 `_payload()` 一处，接官方文档核对后只改这一个函数。

### 启动

```bash
.venv/bin/uvicorn webapp.server:app --host 127.0.0.1 --port 8666
```

macOS 也可直接双击 `m0/启动界面.command`（自动建环境、装依赖、起服务、开浏览器）。

- 用户界面：http://127.0.0.1:8666
- 管理后台：http://127.0.0.1:8666/admin （Basic 认证，用户名 `admin`）

## 智能体接口

`POST /api/agent/submit`，Header `Authorization: Bearer <DIRECTOR_AGENT_TOKEN>`：

```json
{
  "sentence": "蓝色便携水杯，300ml，单手开盖",
  "price": "19.9",
  "size": "standard",
  "orientation": "vertical",
  "persona_authorized": true,
  "image": "data:image/png;base64,...",
  "persona": "data:image/png;base64,..."
}
```

返回 `{job_id, status_url, video_url}`；轮询 `status_url` 至 `stage == "done"` 后取 `video_url`。

一键换人物重做（网页端同款能力）：`POST /api/jobs/{job_id}/persona`（multipart：`persona` 图片文件 ＋ `persona_authorized=true`），复用原任务的商品图、文案与设置重新制作。

## 测试

```bash
cd m0
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests -q
```

25 项测试使用假 Comfy 客户端与真实 FFmpeg 合成，覆盖：接口鉴权与公开/内部数据隔离、图片与宣称校验、横竖版与标准/高清、真实渲染（字幕/价格贴片/音频/10 秒时长）、编导提示词约束、人物参考与换脸重做、质检门重拍（含防「假喝」）、GPU 排队、网络中断、智能体接口、重启保护。

## 安全与合规说明

- 服务仅绑定回环地址，拒绝公网代理与跨站请求；不要暴露到公网
- `.env` 与 `m0/.private/` 含密钥与管理员密码，已被 `.gitignore` 排除，**永不提交**
- 管理后台展示内部提示词与远端任务 ID，仅限维护人员
- AI 生成内容带显式标识，写入画面与 MP4 元数据；标识不代表任何监管认证
- 上传人物照片必须已获得本人肖像使用授权（产品强制勾选）；请遵守所在地区 AIGC 与广告法规发布成片
- 发布前请人工核对商品外观、字幕与价格——AI 生成可能改变包装细节

## 许可证

[MIT](LICENSE)
