# P0 直播原型

目标：依据 [当前契约](../specs/tuber-studio.md) 跑通大肥鱼 Mock → MiniCPM → 语音／预录 → overlay → WebUI，Jetson 正常使用。

输入：`tmp/GIFTuber_Studio_SRS_v0.1.md`；用户补充原型先使用 DeepSeek 鲸鱼娘／大肥鱼，后续再替换相似内容。

| 工作 | 输入需求 | 当前状态／下一步 |
| --- | --- | --- |
| 项目与持久化／角色版本 | CHAR-01/02/03, MEM-01/02 | 实现 SQLite 与角色卡 |
| 事件与播放状态 | LIVE-01/03/04/08, AUDIO-02/06 | 实现有界队列、幂等、取消、播放确认 |
| 实际模型与声音 | AUDIO-01/03 | Jetson SSH 可用、镜像已存在；获取候选权重并验证 |
| UI／素材／overlay | MEDIA-01/02, UI-01/02/04 | 实现独立播放器与管理操作 |
| 验证与部署说明 | P0, NFR-07/08/09/10/12 | 聚焦回归、浏览器、真实设备短句；完成后同步未验边界 |

Jetson 独立目录 `/home/weizheng/code/Tuber_Studio`，使用新端口；不得改动既有 videoanalyze-app、jetson-research 服务。P1/P2 不在本次闭环完成声明内。
