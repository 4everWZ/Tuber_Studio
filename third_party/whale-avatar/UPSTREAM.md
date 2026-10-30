# 大肥鱼 Q 版角色素材

作者：YunYueSama  
仓库：<https://github.com/YunYueSama/codex-deepseek-pet>

使用版本：`7661c8b304c5400701f91da01b1a643a207331de`。
完整许可见 [LICENSE](LICENSE)，参考作品边界见 [ASSET_LICENSE.md](ASSET_LICENSE.md)。

本项目将完整角色图集中的帧裁出、缩放至 384px，并转换成六种透明 GIF。
六种状态统一使用蓝紫渐层长发、圆脸蓝眼、鲸鳍、单条鲸尾与深蓝女仆装／白色鲸鱼围裙。
说话状态使用招呼动作，不代表口型同步。声音素材独立维护。

直播唱歌另外复用同一版本`expressions.png`图集的第12／13帧（从0开始），裁出并缩放为384px透明`sing_open.png`／`sing_closed.png`。两帧分别用于人声活动时的张嘴与无声时的闭嘴，未生成新角色图片；具体源帧及来源保存在角色manifest中。上游署名、许可和社区参考边界继续适用。

重建方式：`uv run --with pillow python tools/import_whale_avatar.py`。原图下载至忽略的 `tmp/`。

仅重建唱歌两帧：`uv run --with pillow python tools/import_whale_avatar.py --singing-only`。
