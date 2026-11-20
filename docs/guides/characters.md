# 角色卡、音色与形象

角色、音色和素材是独立引用；评论与直播共用接口，更换OC不改调度器或Provider。角色由[角色规范](../specs/foundation/runtime-and-characters.md)维护，关系及记忆见[记忆规范](../specs/foundation/memory.md)。

工作室默认提供大肥鱼角色卡。新建角色由系统分配稳定 ID，人设、说话风格和台词示例从空白开始，音色和形象使用项目默认绑定。

1. 在「角色工作室 → 角色卡」通过「新建角色卡」填写角色名称。列表按首次创建时间排列，最早创建的在最上方。内部 ID 可在「高级 JSON 源码」查看。
2. 在「编辑角色卡」修改名称、身份、人格、示例对白和口吻。保存覆盖当前卡片，当前激活的角色也可以改名。切换时选择要用的卡片并点击激活。有未保存修改时可以直接「保存并激活」。
3. 用 `voice_profile` 选择已登记的声音。新装用 `whale_reference`，已选瑶瑶的角色沿用 `yaoyao_reference`。两者用各自录音生成新句。固定试听 `whale_sample_v1` 只播放登记的原句，已有 `external_api` 保留原来的声音选择。要用 Omni 试听新句，须明确选用参考音色。[声音清单与许可](../../config/voices.json)
4. 图片放assets/sea_salt/，建立manifest.json，再将avatar_manifest指向assets/sea_salt/manifest.json。复制时可暂复用旧清单，换原创素材后独立替换。

最小清单示意：

    {
      "entries": [
        {"id":"idle","tag":"idle","path":"assets/sea_salt/idle.png","source":"原创","license_status":"owned","character_id":"sea_salt"},
        {"id":"talk","tag":"talking","path":"assets/sea_salt/talk.gif","source":"原创","license_status":"owned","character_id":"sea_salt"}
      ]
    }

常用标签为 `idle`、`talking`、`cheerful`、`angry`、`confused`、`shy` 和 `gift`，缺图时回退待机。为 WAV 音效填写对应文本，Mock 事件通过 `metadata.asset_id` 引用已登记素材。歌曲在直播曲库中维护。

角色音色和素材清单分别登记。更换参考后用相同台词和一条新句试听，检查原文和声线。激活角色或编辑当前角色会停止旧待播队列，已有草稿和回执继续保留来源。编辑当前角色后，重新启用对应的自主运营权限。

## 角色输入

在「角色工作室 → 角色卡 → 核心人设与性格」编辑「角色自述（常驻）」和「说话风格（常驻）」。自述用角色自己的话写明身份，风格写平常怎样开口。两项各限单行 120 字，对应高级 JSON 顶层的 `self_description` 和 `speaking_style`。住房、饮食喜好、往事和示例对白可以写在完整设定中。

常驻资料始终含当前角色名。某项留空时，读取旧卡相应的 `core.identity` 或 `style.instructions`，只采用开头能辨认的完整身份或口吻分句，最长 80 字。例如「你是蓝发鲸鱼娘，住在海底小屋」可提供「你是蓝发鲸鱼娘」。过长或无法判断的分句会省略。读取不会改写旧卡，原有身份、背景和表达规范仍可在原栏目编辑。

选中角色卡后，在左侧列表下方的「B 站号主」填写 UID 或个人空间链接，点击「查询账号」预览头像、昵称和 UID，点击昵称可打开个人空间。也可以选「使用当前登录账号」。点击「保存角色」完成绑定，左侧角色卡列表会显示号主昵称。清空此项并保存可解除绑定。观众问「你是谁家的鱼」时，角色可按自己的口吻介绍所绑定的号主。

直播、评论、私信和动态读取当前选中的角色卡。模型由配置的推理服务提供，切换服务不改变角色卡。角色预设文件的历史由 Git 管理。 [角色输入规范](../specs/foundation/runtime-and-characters.md#角色生成方式)

## 初始大肥鱼内容与替换

默认六种 GIF 统一使用 [YunYueSama 的大肥鱼 Q 版图集](https://github.com/YunYueSama/codex-deepseek-pet)：蓝紫渐层长发、圆脸蓝眼、鲸鳍、单条鲸尾、深蓝女仆装和白色鲸鱼围裙。完整角色帧经裁出、缩放和 GIF 编码，待机、招呼、开心、生气、困惑、礼物保持同一套造型。招呼动画用于说话状态，不表示口型同步。作者署名和完整素材条款见 [素材来源](../../third_party/whale-avatar/UPSTREAM.md)，逐项来源与动作帧在 assets/whale/manifest.json；声音引用独立维护。可用 `uv run --with pillow python tools/import_whale_avatar.py` 重建图片，原图留在忽略的 tmp/。

唱歌的`sing_open`／`sing_closed`复用同一图集的现成张嘴／闭嘴帧；新OC可在自身manifest提供同名标签，缺失则回退待机。两张图按人声活动数据切换开合。仅重建这两帧可加`--singing-only`。

大肥鱼录音署名尹紫鹭幻想社，使用时保留来源并遵守 CC BY-NC 4.0。瑶瑶参考另有来源、作者和权利人，清单中单独标注。打赏直播等商业用途须核对相应授权。[大肥鱼声音数据集](https://www.modelscope.cn/datasets/YinZilu/da-fei-yu-girl-voice-zh) · [逐项来源、署名与条款](../../config/voices.json)

音色在配置中分别记录来源和适用场景。更换声音时同步更新资料，并用目标场景试听。
