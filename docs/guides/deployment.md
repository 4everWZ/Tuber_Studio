# 部署与启动

## 项目环境

在项目根目录使用Python3.12和uv，不向系统或base环境安装依赖：

```powershell
uv sync --python 3.12
uv run python -m tuber_studio --port 8020
```

管理令牌只在 `runtime/admin-token`，登录页填入其内容；不把令牌放URL。模型配置读取 `runtime/studio.json`，不存在则用 `config/studio.example.json`。更换模型／TTS地址后重启应用。离线固定台词诊断可设置 `llm.kind=demo`、`tts.kind=prerecorded`，并选预录音色；该模式不代表真实推理或128K能力。

## Jetson

项目固定服务端口为LLM9010、TTS9011、工作台9020，均绑定回环；现有其他项目不需要停止。当前机器连接别名 `ssh jetson`，项目路径 `/home/weizheng/code/Tuber_Studio`。代码和工具放项目目录，模型／venv／配置／数据库均放其runtime。

```bash
bash tools/jetson/start-llm.sh
bash tools/jetson/tts/deploy.sh
bash tools/jetson/start-studio.sh
```

脚本不会默认替换同名LLM／后台容器；已有服务使用 `docker start tuber-llm`／`docker start tuber-studio`。更新Python依赖后重启后台；入口会用项目uv执行 `uv sync --locked --no-dev`，确保依赖与lock一致。TTS工具的 `--replace` 只替换本项目、且挂载路径匹配的TTS容器；准备阶段旧服务保持运行。权重和依赖齐备后可用 `bash tools/jetson/tts/deploy.sh --offline --replace` 重建，不重新下载。

初始LLM镜像为 `ghcr.io/4everwz/jetson-llama-cpp:r36.4-cu128-u24.04-sm87-d749821`，模型为官方MiniCPM5-1B Q4_K_M。启动脚本使用128K、q8 KV、单slot、FlashAttention、禁用自动缩窗。受限网络可显式设 `TUBER_HF_BASE=https://hf-mirror.com`；默认仍为官方Hugging Face。

TTS使用官方GPT-SoVITS `20240821v2`、项目独立venv及已存在CUDA PyTorch镜像。工具以导入兼容层处理旧typing导入与模型加载峰值，不改训练／解码算法或最终FP16权重。源码、BERT／HuBERT、大肥鱼权重均在runtime，下载来源与具体版本在工具中。参考音频服务端路径为 `/workspace/tuber/runtime/tts/ref/dafeiyu_ref.wav`。

Windows连接Jetson后台：

```powershell
powershell -File tools/windows-tunnel.ps1
```

随后打开 [工作台](http://127.0.0.1:8021)，通过 `ssh jetson 'cat /home/weizheng/code/Tuber_Studio/runtime/admin-token'` 在本机终端取得管理令牌。脚本默认本地8021，可通过 `-LocalPort` 更换转发端口。

也可以后台运行在Windows、模型在Jetson：转发19010→9010及19011→9011，配置文件中把两个Provider地址改成对应本地端口。此时资源栏统计的是Windows后台进程；设备资源应另看Jetson的tegrastats／vmstat。

## 第一次使用

1. 评论工作台刷新热门视频，选视频或填BV号读取评论；也可导入CSV／JSON。选择评论生成草稿后，审核、编辑、复制。
2. 回复设置默认关闭自动浏览／自动回复。按需配置关注视频、轮询频率和限额。账号凭证只在设置密码框写入本地；无凭证时仍可浏览和审核复制。
3. 语音页选择动态或预录音色试听。直播控制打开Mock会话；从设置复制独立overlay链接，放OBS浏览器来源或单独浏览器窗口。只有一个播放页拥有播放权，管理预览不重复发声。
4. 浏览器阻止自动播放时，按播放页提示启用音频，再注入新事件；失败任务不会偷偷重播。暂停阻止后续任务；静音立即取消当前播报。

管理和overlay凭证分权；不要把overlay链接当管理令牌。真实发布失败或结果不确定应查看草稿回执／错误，不能把审核通过当已发布。

## 已测设备边界

Orin Nano 8GB上的当前profile已实际配置131072 tokens，并在TTS共驻时完成一次无缓存、无截断的120000-token输入：prefill约339秒、输出4tokens。该证据支持长输入可执行，不支持长文召回质量或实时响应。

动态TTS短句音频4.56秒，冷请求约17.6秒、热请求约5.1秒。模型可共驻，但观测到少量换页，未完成连续100句／两小时稳定性及完整长历史语义评估。实时性能遇到瓶颈应先检查长注意力与prefill、压缩耗时、参考音频缓存及换页，不能靠无限增加超时或把窗口降到128K以下掩盖问题。
