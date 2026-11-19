# 运营草稿配图

角色工作室的图库接受 PNG、JPEG、WebP 和 GIF。可多选文件导入，各文件独立登记，重试不会重收已处理的文件。批量填写的标签、画面描述和署名应用到本批，之后可逐张修改标签与描述，原图与来源保持绑定。

配图决定按正文和渠道上下文，从启用且可读的图库中先筛选少量候选，优先匹配题材标签、名称和画面描述。后台模型只能从本次候选中选图，也可选纯文字。图库开启时，关闭生图仍可使用现成图片。

视频一级评论、私信和动态通过 `runtime.operation_images` 排队。业务域从实际草稿构造 `tuber_studio.operation_images.ImageScope(account_id, character_id, channel, source_id)`（使用关键字参数，也接受同字段字典）。渠道取 `comment`、`direct_message`、`dynamic`。账号、角色、来源以及私信收件人由渠道记录确定，发送者的文字仅作为材料。图片权限独立于 Avatar 和 Pets 的演出设置。

| 服务方法 | 结果 |
| --- | --- |
| `decide(scope, request_key=..., context=..., draft=..., character_version=...)` | 同步登记，立即返回 `job_id` 和状态。角色版本可选，已有版本的草稿应传其快照版本。同一作用域和键恢复原任务；不同材料或改稿使用新键。 |
| `status(scope, job_id)` | `queued / deciding / submitting / running / text_only / gallery / ready / failed`。另返回网关实报 `remote_state`、`seed`，已选参考的来源、版本和 SHA-256，完成时给 `artifact_id`，失败时给 `error`。 |
| `read(scope, artifact_id)` | 同一作用域内读取原始 PNG `content`、`mime_type`、`width`、`height`、`sha256`、模型、参考版本与来源。可重复读取。 |

每个 Core 写者运行一个配图队列。楼内回复在入队前按目标获得文字能力。设置页的「运营配图」为视频一级评论、私信和动态共用两个独立开关「生图」与「图库」。两者皆关时只写正文。新装仅启用图库；已有渠道设置合并时，只保留三个渠道均允许的来源。后台 Responses 在同一次决定中从已启用的图库条目、已绑定原稿的 Q／常规版和保留正文中选择。图库图按 ID 和内容摘要绑定草稿。选图库或无需新图时，工坊不提交图片推理。没有工作站生图接口的旧部署仍可用原图库功能。生图使用普通应用凭据访问工作站 `/v1/images/jobs`，不持有 Studio 的前台 generation lock。当前部署固定 FLUX.2-klein-4B，无调用方模型选择字段。共享 GPU 的释放、优先级和队列由工作站管理。网关返回 429 时依照 Retry-After 等待；Core 已记录远端任务 ID 时重启后继续轮询。如果提交结果未知，任务保留失败原因以免重复生成。

入队时按角色、Q／mature 和所选版本绑定参考摘要与来源。工作站业务卷按角色和版本在 `artwork-references` 中保存上传件，`active` 指向当前选择，`default` 指向号主指定的默认件。「恢复默认」清除当前选择。未指定默认件的大肥鱼角色使用 `private-artifacts/artwork-defaults/{original.png,Q_original.png}`。实际发起推理时按入队时的 digest 取回原字节，再核对摘要。外部请求只得到经过 Base64 编码的内联参考和场景文字。任务工具没有路径、URL 或文件浏览参数。

`operation_image_jobs` 保存决策输入、草稿、工坊任务及远端 ID，`operation_image_references` 保存任务绑定，`operation_image_gateway_receipts` 保存工作站状态，`operation_image_artifacts` 保存经过解码验证的 PNG 和摘要。工件入库与任务变成 ready 在同一事务内。远端临时结果到期以后，渠道仍可用工坊工件 ID 回读。发布审核、目标绑定、上传与送达回执由渠道负责。[工作站实测](../../../evaluations/results/operations-artwork-20261119.md)
