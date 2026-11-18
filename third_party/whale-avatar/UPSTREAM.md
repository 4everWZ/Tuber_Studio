# 大肥鱼 Q 版角色素材

作者：YunYueSama  
仓库：<https://github.com/YunYueSama/codex-deepseek-pet>

使用版本：`7661c8b304c5400701f91da01b1a643a207331de`。
完整许可见 [LICENSE](LICENSE)，参考作品边界见 [ASSET_LICENSE.md](ASSET_LICENSE.md)。

本项目将完整角色图集中的帧裁出、缩放至 384px，并转换成六种透明 GIF。
六种状态统一使用蓝紫渐层长发、圆脸蓝眼、鲸鳍、单条鲸尾与深蓝女仆装／白色鲸鱼围裙。
说话状态使用招呼动作，不代表口型同步。声音素材独立维护。

对话首页的默认大肥鱼宠物复用待机图；减少动态效果时使用静态表情。八张内置表情的逐图来源、帧号和处理方式见 [表情清单](../../assets/whale/memes/manifest.json)。其他仓库的图片可通过工作台「素材与表情包」单独导入，填写实际作者、原图链接、许可名称、许可链接及可用的版本号；有链接不等于工坊已经核验授权。宠物选图只展示已启用、文件可用、含作者与许可名称且来源／许可可通过 HTTPS 链接查看的条目。选图不会发布或发送图片。

直播唱歌另外复用同一版本`expressions.png`图集的第12／13帧（从0开始），裁出并缩放为384px透明`sing_open.png`／`sing_closed.png`。两帧分别用于人声活动时的张嘴与无声时的闭嘴，未生成新角色图片；具体源帧及来源保存在角色manifest中。上游署名、许可和社区参考边界继续适用。

重建方式：`uv run --with pillow python tools/import_whale_avatar.py`。原图下载至忽略的 `tmp/`。

仅重建唱歌两帧：`uv run --with pillow python tools/import_whale_avatar.py --singing-only`。
