# 运营草稿配图

评论、私信和动态通过 `runtime.operation_images` 排队。业务域从实际草稿构造 `tuber_studio.operation_images.ImageScope(account_id, character_id, channel, source_id)`（使用关键字参数，也接受同字段字典）。渠道取 `comment`、`direct_message`、`dynamic`。账号、角色、来源以及私信收件人由渠道记录确定，发送者的文字仅作为材料。图片权限独立于 Avatar 和 Pets 的演出设置。

| 服务方法 | 结果 |
| --- | --- |
| `decide(scope, request_key=..., context=..., draft=..., character_version=...)` | 同步登记，立即返回 `job_id` 和状态。角色版本可选，已有版本的草稿应传其快照版本。同一作用域和键恢复原任务；不同材料或改稿使用新键。 |
| `status(scope, job_id)` | `queued / deciding / submitting / running / text_only / ready / failed`。另返回网关实报 `remote_state`、`seed`，已选参考的来源、版本和 SHA-256，完成时给 `artifact_id`，失败时给 `error`。 |
| `read(scope, artifact_id)` | 同一作用域内读取原始 PNG `content`、`mime_type`、`width`、`height`、`sha256`、模型、参考版本与来源。可重复读取。 |

每个 Core 写者运行一个配图队列。后台 Responses 根据角色卡、本渠道材料和草稿选择纯文字或场景描述与参考版本。生图使用普通应用凭据访问工作站 `/v1/images/jobs`，不持有 Studio 的前台 generation lock。当前部署固定 FLUX.2-klein-4B，无调用方模型选择字段。共享 GPU 的释放、优先级和队列由工作站管理。网关返回 429 时依照 Retry-After 等待；Core 已记录远端任务 ID 时重启后继续轮询。如果提交结果未知，任务保留失败原因以免重复生成。

入队时按角色、Q／mature 和所选版本绑定参考摘要与来源。默认原稿为工作站业务卷 `private-artifacts/artwork-defaults/{original.png,Q_original.png}`。上传替代由参考设置服务私有持久化。实际发起推理时按入队时的 digest 取回原字节，再核对摘要。外部请求只得到经过 Base64 编码的内联参考和场景文字。任务工具没有路径、URL 或文件浏览参数。

`operation_image_jobs` 保存决策输入、草稿、工坊任务及远端 ID，`operation_image_references` 保存任务绑定，`operation_image_gateway_receipts` 保存工作站状态，`operation_image_artifacts` 保存经过解码验证的 PNG 和摘要。工件入库与任务变成 ready 在同一事务内。远端临时结果到期以后，渠道仍可用工坊工件 ID 回读。发布审核、目标绑定、上传与送达回执由渠道负责。[工作站实测](../../../evaluations/results/operations-artwork-20261119.md)
