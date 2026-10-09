# MiniMax 官方 API：不部署视频模型

本地网页、编导和 FFmpeg 仍需安装；视频模型运行在 MiniMax 云端，无需 ComfyUI、SSH、模型权重或本地 GPU。仅支持单用户本机访问，不提供可直接公开部署的 SaaS。

## 私密配置

只在本机 `m0/.env` 配置（文件权限建议 `600`，已被 Git 忽略）：

```dotenv
VIDEO_ENGINE=h3api
H3_API_KEY=
H3_API_BASE_URL=https://api.minimax.cn
H3_API_MODEL=MiniMax-H3
H3_API_RESOLUTION=768P
DEEPSEEK_API_KEY=
```

空值处自行填写密钥，不要提交或分享到聊天。视频 Key 也可用 `MINIMAX_API_KEY`；若两者都设置，`H3_API_KEY` 优先。文本编导可改用 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`；视觉识别可选且独立配置。视频 Key 不会自动传给其他模型服务。

| 模型 | 分辨率 | 官方允许时长 | 本工具每镜时长 |
| --- | --- | --- | --- |
| `MiniMax-H3` | `768P`、`2K` | 4–15 秒整数 | 5 秒 |
| `MiniMax-H3-Max` | `480P`、`768P` | 5–15 秒整数 | 5 秒 |

输出画幅为 `9:16` / `16:9`；页面标准/高清只决定 FFmpeg 成片尺寸，不覆盖 API 分辨率。原生模型音频当前不保留，成片使用程序生成的原创配乐与字幕。

已有配置不会被安装脚本覆盖。本地工作流继续用 `VIDEO_ENGINE=comfyui`。旧实验 API 配置请改为上述官方 base URL；自定义提交/查询/文件检索路径已移除，固定使用官方 V2 协议。

## 请求与素材边界

- `POST /v2/video_generation`：`model`、`content`、`resolution`、`duration`、`ratio`；不发送 ComfyUI 节点、任意宽高、种子或旧 `prompt_tags`。
- 第一镜：商品参考图 + 可选人物参考图；第二镜：商品参考图 + 第一镜收尾约 2.2 秒视频。图片角色为 `reference_image`，视频为 `reference_video`，不混入首尾帧角色。
- 所有素材来自显式登记的本地文件，Base64 随请求传输；客户端不接受任意外部素材 URL，不把私密配置文件当素材读取。
- 本工具接收静态 JPG/PNG/WebP，清除元数据，必要时等比缩放和白色补边；不裁剪商品。云端图片两边至少 256px、最多 5760px，比例 0.4–2.5，单图不超过 30 MB；本机另限 2400 万像素。
- 连续性视频使用 MP4/H.264、无音轨、24fps；客户端也支持合规的 MOV/H.265。校验 2–15 秒、256–5760px、比例 0.4–2.5、23.976–60fps 和 50 MB。完整 JSON 不超过 64 MB，提示词不超过 7000 字符。
- 云服务将收到提示词、商品图、人物照与上一镜片段。只上传自有/获授权素材，按平台隐私条款处理，不宣称素材始终不离开本机。

## 计费、失败与恢复

使用自己的 MiniMax 账号与额度；开通模型、价格和余额以平台为准。基础成片为两次 5 秒生成，视觉质检最多每镜再拍一次，最多四次提交；参考视频/分辨率等可能影响费用。本版本未实现硬费用上限，不自动购买额度。

提交后立即保存 `task_id`，每 10 秒查询 `GET /v2/query/video_generation/{task_id}`。仅接受匹配 ID 的视频生成任务：`queued` / `running` 等待，`succeeded` 读取 `task.content.url`，`failed` / `cancelled` 明确结束。明确的参数/权限/余额拒绝可重新提交；网络超时、5xx、缺失 ID、未知状态或异常响应不能推断任务没创建，会进入待确认，不自动重发。重拍时清除上一请求 ID，不能拿旧任务的成功状态解除新请求的不确定状态。

平台仅可查询最近 7 天任务，下载地址有时效。过期后应查询同一个任务刷新地址，不应通过新生成刷新链接。超出查询窗口或仍无任务 ID 时，管理员须先到平台核实，不得将查询失败当作队列空闲。`/admin` 的解除阻塞只承认已有 ID 的明确终态，不取消其他任务。

所有 API 通信必须 HTTPS，拒绝重定向与系统代理/自动 netrc 凭据。产物 CDN 用独立无认证连接，不发送 MiniMax Key，默认仅允许精确主机 `video-product.cdn.minimax.io`、`cdn.hailuoai.com`、`filecdn.minimax.chat` 及配置的 API 主机。新主机请先核实，再加 `H3_API_DOWNLOAD_HOSTS`；不放开通配域名。远端响应正文不写入诊断，避免反射密钥、素材或签名下载链接。

## 验证边界

`doctor.py` / `doctor.py --online` 在 API 模式只检查配置与本机依赖，不调用 ComfyUI，也不生成付费视频。检查通过不证明 Key 有效、余额充足或模型已开通。自动化测试采用官方格式的模拟响应和真实 FFmpeg，验证协议、恢复、下载安全与 10 秒成片；不宣称真实云端生成质量已验收。

官方文档（协议核对日期：2026-10-09）：[创建任务](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create)、[查询任务](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-query)、[视频生成指南](https://platform.minimaxi.com/docs/guides/video-generation)。
