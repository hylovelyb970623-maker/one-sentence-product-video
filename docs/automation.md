# 本地自动化与批量生产

设置至少 32 字符的独立随机 `DIRECTOR_AGENT_TOKEN`；调用方通过环境变量读取。HTTP 头为 `Authorization: Bearer <token>`，不要将真实令牌写入导出工作流、命令参数或截图。

| 方法与路径 | 行为 |
| --- | --- |
| `POST /api/agent/submit` | 提交真实生成，成功 202 |
| `GET /api/agent/jobs/{job_id}` | 总体状态 |
| `GET /api/agent/jobs/{job_id}/video` | 下载成片，未完成 404 |

均要求 Bearer。未配置或过短返回 503，认证失败 401。服务仍仅绑定回环地址；普通网页接口保留本机用户使用方式，不提供多租户授权。

提交 JSON：必需 `sentence`（1–160 字事实）和 `image`（PNG/JPEG/WebP base64 data URL）；可选 `price`、`size`（standard/hd）、`orientation`（vertical/horizontal）、`persona`、布尔值 `persona_authorized`。人物照必须确认肖像授权。每张图最多 12MB，请求最多 34MB。

返回 `job_id`、相对路径 `status_url`、`video_url`。状态为 queued、planning、generating、rendering、done、error、attention；attention 表示远端结果不确定，需管理员核对，不能盲目重发。

## 幂等与恢复

自动化应发送 `Idempotency-Key`，使用 16–128 位字母、数字、下划线或连字符。相同键与相同 JSON 返回原任务；内容变化返回 409。映射随任务持久化，删除任务后不再保证幂等。重试已失败任务也只返回原任务，确需新生成才使用新键。

当前一个输出目录同一时间只生成一个任务，忙碌返回 409。使用单 uvicorn worker，批量客户端串行提交。

清单见 `examples/batch.example.json`，每项包含唯一 id、商品事实与相对清单位置的图片路径，最多 100 项；横竖版分别计一次真实生成，不要在清单存令牌。

```bash
m0/.venv/bin/python m0/batch.py examples/batch.example.json
m0/.venv/bin/python m0/batch.py examples/batch.example.json --submit --output m0/out/batch
```

默认校验清单和图片，不消费模型，商品文案仍由服务器审核。首个失败、待确认、连接中断或等待超过两小时即停止；已提交任务继续运行，不被取消。

客户端先持久化幂等键再请求，避免响应丢失造成重复生成。中断后使用相同 `--state` 文件重跑；完成文件经 SHA-256 校验后跳过，其余恢复状态或下载。同一 ID 更改内容会拒绝继续。

状态默认在清单旁 `batch-state.local.json`，仅保存 ID、键和摘要，不存 Key 或图片，但仍关联私有任务，不应公开。不同批次使用不同状态文件。当前没有多 GPU 调度或公网回调。
