# ComfyUI 工作流接入

`ref2va.json` 用于当前 Web 两镜链路：首镜商品图与可选人物图，次镜商品图加首镜收尾参考视频。`fl2va.json` 保留首帧工作流适配函数，当前 Web 不使用它生成第二镜。

先在自己的 ComfyUI 验证 H3 工作流，再导出 **API 格式** JSON（开发者模式的 Save API Format）。普通 UI 导出包含编辑器和可能的本机素材信息，不能直接交给此适配器，也不应直接公开。

复制 `config.example.json` 为 `config.local.json`，修改模板路径与节点编号，然后在 `m0/.env` 中设置 `DIRECTOR_WORKFLOW_CONFIG`。相对模板路径以该配置文件所在目录为准，配置路径本身相对启动工作目录；推荐使用本机绝对配置路径，但不要提交它。

| 角色 | 默认节点 | 输入要求 |
| --- | --- | --- |
| generator | 3 | H3 的 prompt、width、height、length 与 ref_images / ref_videos |
| product_image | 18 | LoadImage.image |
| person_image | 28 | 可选 LoadImage，按需注入，不得与其他角色冲突 |
| reference_video | 13 | LoadVideo.file |
| video_components | 16 | GetVideoComponents 的第 0 输出 |
| seed | 21 | RandomNoise.noise_seed |
| output | 17 | SaveVideo.filename_prefix |
| first_frame | 27 | FL2VA 的 LoadImage.image |

只调整节点 ID 时，映射即可完成适配；改变节点类型、输入名或模型协议，需要修改适配器并添加测试。不会自动改写所有模型协议。模型名称、LoRA 与生成参数由使用者的已授权工作流决定。

运行 `python doctor.py --online` 只读检查节点、模型选择器与队列。它不会提交工作流，不能保证权重真实性、许可、显存充足或生成质量。发布示例去除图片/音频私人文件名和旧提示词，不附权重、素材或原始 UI 快照。
