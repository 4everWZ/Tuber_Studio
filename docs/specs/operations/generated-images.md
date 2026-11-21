# 运营草稿配图

角色工作室的图库接受 PNG、JPEG、WebP 和 GIF。可多选文件导入，各文件独立登记，重试不会重收已处理的文件。批量填写的标签、画面描述和署名应用到本批，之后可逐张修改标签与描述，原图与来源保持绑定。

配图决定按正文和渠道上下文，从启用且可读的图库中先筛选少量候选，优先匹配题材标签、名称和画面描述。后台模型只能从本次候选中选图，也可选纯文字。图库开启时，关闭生图仍可使用现成图片。

视频一级评论、私信和动态通过 `runtime.operation_images` 排队。业务域从实际草稿构造 `tuber_studio.operation_images.ImageScope(account_id, character_id, channel, source_id)`（使用关键字参数，也接受同字段字典）。渠道取 `comment`、`direct_message`、`dynamic`。账号、角色、来源以及私信收件人由渠道记录确定，发送者的文字仅作为材料。图片权限独立于 Avatar 和 Pets 的演出设置。

| 服务方法 | 结果 |
| --- | --- |
| `decide(scope, request_key=..., context=..., draft=..., character_version=...)` | 同步登记，立即返回 `job_id` 和状态。角色版本可选，已有版本的草稿应传其快照版本。同一作用域和键恢复原任务；不同材料或改稿使用新键。 |
| `status(scope, job_id)` | `queued / deciding / awaiting_choice / text_only / comment_card / gallery / submitting / running / ready / failed`。返回选择对应的来源指纹或工件 ID、网关状态及失败原因。 |
| `read(scope, artifact_id)` | 同一作用域内读取原始 PNG `content`、`mime_type`、`width`、`height`、`sha256`、模型、参考版本与来源。可重复读取。 |

每个 Core 写者运行一个配图队列。楼内回复和动态评论使用文字。通过共用网关生成正文后，视频一级评论、私信和动态交给同一个后台 Responses 步骤选图。选项包括纯文字、已开启且可读的图库、已开启且绑定参考图的 Q／常规版，以及动态当前来源能生成的原评论图。图库按 ID 和内容摘要绑定，原评论图按保存的快照绑定。工作台的「生图」与「图库」独立控制来源，新装默认启用图库。

纯文字由模型或号主明确选择。候选耗尽、模型决定无法读取、图片失效或撤权时保留正文，自动发送等待。号主可重新选图或明确使用纯文字。改正文会重新绑定该稿的选图请求，审核后按已确认的正文和图片提交。图库与生图均由号主关闭时，新评论和新私信直接走该渠道的文字流程。没有配置共用网关的旧部署仍可使用原图库功能。

生图使用普通应用凭据访问工作站 `/v1/images/jobs`，独立于前台 generation lock。当前部署使用 FLUX.2-klein-4B，共享 GPU 的队列由工作站管理。网关返回 429 时依照 Retry-After 等待，已有远端任务 ID 时重启后继续轮询。提交结果待核对时保留原请求记录。

入队时按角色、Q／mature 和所选版本绑定参考摘要与来源。工作站业务卷按角色和版本在 `artwork-references` 中保存上传件，`active` 指向当前选择，`default` 指向号主指定的默认件。「恢复默认」清除当前选择。未指定默认件的大肥鱼角色使用 `private-artifacts/artwork-defaults/{original.png,Q_original.png}`。实际发起推理时按入队时的 digest 取回原字节，再核对摘要。外部请求只得到经过 Base64 编码的内联参考和场景文字。任务工具没有路径、URL 或文件浏览参数。

`operation_image_jobs` 保存决策输入、草稿、工坊任务及远端 ID，`operation_image_references` 保存任务绑定，`operation_image_gateway_receipts` 保存工作站状态，`operation_image_artifacts` 保存经过解码验证的 PNG 和摘要。工件入库与任务变成 ready 在同一事务内。远端临时结果到期以后，渠道仍可用工坊工件 ID 回读。发布审核、目标绑定、上传与送达回执由渠道负责。[工作站实测](../../../evaluations/results/operations-artwork-20261119.md)
