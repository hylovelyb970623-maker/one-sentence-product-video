# ComfyUI 工作流模板目录

对接本地/远程 ComfyUI（MiniMax H3）时，把两个工作流的 **API 格式 JSON** 放到本目录：

- `fl2va.json` —— 首尾帧生视频工作流（用于镜头衔接：上游尾帧 → 下游首帧）
- `ref2va.json` —— 全能参考生视频工作流（用于商品/模特一致性）

## 导出方法

1. ComfyUI 设置里勾选 **Enable Dev mode Options**（开发者模式）
2. 打开对应工作流 → 点 **Save (API Format)** 导出 JSON
3. 改名放入本目录，并在 `../.env` 里配置 `COMFYUI_HOST`

## 之后要做的节点映射

拿到 JSON 后确认可变参数的节点 ID（在 `s6_gateway.py` 的 ComfyUIEngine 里配置）：

- 正/负向提示词节点（CLIPTextEncode）
- 参考图/首帧/尾帧输入节点（LoadImage / VHS_LoadImage）
- 种子节点（KSampler）
- 输出节点（SaveVideo / VHS_VideoCombine）

调优工作流时只需重新导出 JSON 替换本文件，网关代码不动。
