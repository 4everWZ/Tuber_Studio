# 创建新OC

角色、音色和素材是独立引用；评论与直播共用接口，更换OC不改调度器或Provider。角色／记忆语义由[角色规范](../specs/runtime-and-characters.md)维护。

1. 「角色」中选择卡片，复制成稳定小写ID（如sea_salt）和新名称。
2. 编辑core的身份／人格／示例对白、style的口吻／字数、rules与lore；保存产生新版本，选择后激活或回滚。Lore配置触发词／标签、优先级和预算时按相应功能实际支持情况使用，未实现的P1能力不视为可用。
3. voice_profile引用config/voices.json中的独立音色；填Provider、版本／权重、参考音频／文本、来源、使用约束和推理参数。参考音频使用容器内实际可读路径，不把宿主路径直接当服务路径。一个TTS服务的全局权重不能在另一角色发声时被偷偷改掉。
4. 图片放assets/sea_salt/，建立manifest.json，再将avatar_manifest指向assets/sea_salt/manifest.json。复制时可暂复用旧清单，换原创素材后独立替换。

最小清单示意：

    {
      "entries": [
        {"id":"idle","tag":"idle","path":"assets/sea_salt/idle.png","source":"原创","license_status":"owned","character_id":"sea_salt"},
        {"id":"talk","tag":"talking","path":"assets/sea_salt/talk.gif","source":"原创","license_status":"owned","character_id":"sea_salt"}
      ]
    }

使用idle／talking／cheerful／angry／confused／shy／gift等有限标签；缺图回退。WAV音效需有准确text，Mock sfx经metadata.asset_id引用，不能填任意磁盘路径。合法music曲库与角色音效分开，歌曲检索结果不自动获得直播播放权。

音色模板和素材清单是项目配置，可进Git；权重、试听和合成缓存留runtime。模型／音色版本、有效参数或参考音频内容改变时缓存失效。激活新角色／版本停止旧待播队列；旧评论草稿保留生成时版本，短期上下文也按版本隔离。

## 初始大肥鱼内容与替换

默认六种 GIF 统一使用 [YunYueSama 的大肥鱼 Q 版图集](https://github.com/YunYueSama/codex-deepseek-pet)：蓝紫渐层长发、圆脸蓝眼、鲸鳍、单条鲸尾、深蓝女仆装和白色鲸鱼围裙。完整角色帧经裁出、缩放和 GIF 编码，待机、招呼、开心、生气、困惑、礼物保持同一套造型。招呼动画用于说话状态，不表示口型同步。作者署名和完整素材条款见 [素材来源](../../third_party/whale-avatar/UPSTREAM.md)，逐项来源与动作帧在 assets/whale/manifest.json；声音引用独立维护。可用 `uv run --with pillow python tools/import_whale_avatar.py` 重建图片，原图留在忽略的 tmp/。

声音参考 [大肥鱼声音数据集](https://www.modelscope.cn/datasets/YinZilu/da-fei-yu-girl-voice-zh) 和 [GPT-SoVITS资源](https://www.modelscope.cn/datasets/sirmn6/da-fei-yu-gpt-sovits)。资源标有CC BY-NC 4.0时保留来源／署名、非商用约束；音频、数据、模型及使用场景分别核对，打赏直播不能自动当作已获商用许可。替换近似或原创资产时更新清单／声音身份，不只替换文件名。

现有原型允许先使用已有内容；商业模式默认拒绝无商业授权音色。该检查不会替运营者取得第三方素材权利，来源变化后也应重新核对。
