# 角色卡、音色与形象

角色、音色和素材是独立引用；评论与直播共用接口，更换OC不改调度器或Provider。角色／记忆语义由[角色规范](../specs/foundation/runtime-and-characters.md)维护。

工作室默认提供大肥鱼角色卡。新建角色时可从当前卡片起步，设置自己的稳定 ID 和名称。

1. 在「角色 → 角色卡与记忆」选择一张卡片，通过「新建角色卡」填写稳定的小写 ID（如 `sea_salt`）和名称。
2. 在「编辑角色卡」调整身份、人格、示例对白、口吻及 Lore。保存覆盖当前卡片。切换时选择要用的卡片并点击激活。
3. voice_profile引用config/voices.json中的独立音色；填Provider、版本／权重、参考音频／文本、来源、使用约束和推理参数。参考音频使用容器内实际可读路径，不把宿主路径直接当服务路径。本地 TTS 使用已加载的音色。换音色后，等服务就绪再试听。
4. 图片放assets/sea_salt/，建立manifest.json，再将avatar_manifest指向assets/sea_salt/manifest.json。复制时可暂复用旧清单，换原创素材后独立替换。

最小清单示意：

    {
      "entries": [
        {"id":"idle","tag":"idle","path":"assets/sea_salt/idle.png","source":"原创","license_status":"owned","character_id":"sea_salt"},
        {"id":"talk","tag":"talking","path":"assets/sea_salt/talk.gif","source":"原创","license_status":"owned","character_id":"sea_salt"}
      ]
    }

常用标签为 `idle`、`talking`、`cheerful`、`angry`、`confused`、`shy` 和 `gift`，缺图时回退待机。为 WAV 音效填写对应文本，Mock 事件通过 `metadata.asset_id` 引用已登记素材。歌曲在直播曲库中维护。

音色配置和素材清单由 Git 管理，权重、试听和合成缓存留在 `runtime/`。更改模型、声音、有效参数或参考音频时更新缓存。激活角色或编辑当前角色会停止旧待播队列，已有草稿和回执继续保留来源。编辑当前角色后，重新启用对应的自主运营权限。

## 角色输入

直播、评论、私信和动态读取当前选中的角色卡。大肥鱼运营档沿用当前配置的 9B 微调模型，换模型后仍使用所选卡片。角色预设文件的历史由 Git 管理。 [角色输入规范](../specs/foundation/runtime-and-characters.md#角色生成方式)

## 初始大肥鱼内容与替换

默认六种 GIF 统一使用 [YunYueSama 的大肥鱼 Q 版图集](https://github.com/YunYueSama/codex-deepseek-pet)：蓝紫渐层长发、圆脸蓝眼、鲸鳍、单条鲸尾、深蓝女仆装和白色鲸鱼围裙。完整角色帧经裁出、缩放和 GIF 编码，待机、招呼、开心、生气、困惑、礼物保持同一套造型。招呼动画用于说话状态，不表示口型同步。作者署名和完整素材条款见 [素材来源](../../third_party/whale-avatar/UPSTREAM.md)，逐项来源与动作帧在 assets/whale/manifest.json；声音引用独立维护。可用 `uv run --with pillow python tools/import_whale_avatar.py` 重建图片，原图留在忽略的 tmp/。

唱歌的`sing_open`／`sing_closed`复用同一图集的现成张嘴／闭嘴帧；新OC可在自身manifest提供同名标签，缺失则回退待机。口型需要独立人声活动数据驱动，两张图本身不代表已经实现音素级口型或演唱能力。仅重建这两帧可加`--singing-only`。

声音参考 [大肥鱼声音数据集](https://www.modelscope.cn/datasets/YinZilu/da-fei-yu-girl-voice-zh) 和 [GPT-SoVITS资源](https://www.modelscope.cn/datasets/sirmn6/da-fei-yu-gpt-sovits)。资源标有CC BY-NC 4.0时保留来源／署名、非商用约束；音频、数据、模型及使用场景分别核对，打赏直播不能自动当作已获商用许可。替换近似或原创资产时更新清单／声音身份，不只替换文件名。

音色在配置中分别记录来源和适用场景。更换声音时同步更新资料，并用目标场景试听。
