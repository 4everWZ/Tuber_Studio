# 大肥鱼默认人格文字来源

`config/characters.json` 中 `whale` 的人格表达参考并改写了以下三份 MIT 项目的文字。名称仍为“大肥鱼”，ID仍为`whale`。这里只说明人格文字来源，图像、表情、声线和其他素材继续沿各自许可；角色原有`license_status`保持不变。

## 来源与许可

| 项目／作者署名 | 固定版本与实际文件 | 保留的许可原文 |
| --- | --- | --- |
| whyever77 / dafeiyu-astrbot contributors，Copyright (c) 2026 | [`7ee692a7479aa2774fa41ff54390ad8bef46d8d3`：persona/大肥鱼-人格.md](https://github.com/whyever77/dafeiyu-astrbot/blob/7ee692a7479aa2774fa41ff54390ad8bef46d8d3/persona/大肥鱼-人格.md) | [MIT](LICENSE-dafeiyu-astrbot.txt) |
| SAKURAfan1023 / Dafeiyu Contributors，Copyright (c) 2026 | [`ff2bb2f0baf99e449b529f897e7db0d6561321de`：QQPersonality.swift](https://github.com/SAKURAfan1023/dafeiyu-bot/blob/ff2bb2f0baf99e449b529f897e7db0d6561321de/Sources/BotCore/QQPersonality.swift)、[QQPersona.swift](https://github.com/SAKURAfan1023/dafeiyu-bot/blob/ff2bb2f0baf99e449b529f897e7db0d6561321de/Sources/BotCore/QQPersona.swift) | [MIT](LICENSE-dafeiyu-bot.txt) |
| Kaalia0912 / dsh-whale-musume-persona contributors，Copyright (c) 2026 | [`0fbdcf217e3bf94e35fef36f7ac56134b704529b`：agent.cordis.yml，persona段落](https://github.com/Kaalia0912/dsh-whale-musume-persona/blob/0fbdcf217e3bf94e35fef36f7ac56134b704529b/agent-presets/whale/agent.cordis.yml#L30-L53) | [MIT](LICENSE-dsh-whale-musume-persona.txt) |

## 来源到本项目设定的改写

| 来源中的主题 | 本项目采用的表达与位置 |
| --- | --- |
| AstrBot“形象与性格”：聪明、爱面子、嘴硬心软，有自己的偏好与看法 | `core.identity/traits/interaction`：机灵又有小脾气，坦率分享偏好，熟悉后更亲近；关心落在回应与行动里。 |
| AstrBot“聊天节奏”“分寸与情绪”：一到三句、可追问也可不问，按当下情绪接话 | `style.instructions`与`rules`：自然短句、按话题展开；难过时倾听，认真托付时给出下一步。 |
| QQPersonality的teasing、gentle、tsundere、energetic描述，以及QQPersona的轻松互动与夸奖反差 | `core.traits`与夸奖／调侃示例：会得意、会不好意思、能认栽；好奇与有来有回的小玩笑。未整体引入其他独立性格模式。 |
| DSH persona：聪明爱偷懒但认真帮忙，夸奖时尾巴摇动，尾巴抱枕的亲昵玩笑 | `core.traits`、夸奖／抱枕示例与相关Lore：尾巴泄露心情，亲近带点别扭；名称与自称仍按本项目设置。 |
| 原应用已有的海底串门、厨房、白饭与摸鱼主题，加上DSH的海洋背景 | `core.background`与5条Lore：海底小屋、热饭、软垫、气泡、贝壳灯和小故事。新增具体场景与道具是本项目创作，不声称源卡已有这些完整设定。 |

8条对白是本项目编写的语气演示，第一条保留原应用例句，其余结合上述主题新写；不是上游逐字引句，也不是模型实测结果。前3条覆盖日常、一般情绪和认真托付，后续包含摸鱼、夸奖、尾巴抱枕、调侃与推进想象故事。未把评测中的具体视频、题目或目标答案写入默认卡。

仅采用角色与交流主题。上游编码助手配置、工具权限、推理格式、强制服从、账号身份和发送操作等内容不属于本次人格文字改编。来源、版本与许可保存在本说明及角色`sources`中，Prompt只组装角色表达与命中的Lore。
