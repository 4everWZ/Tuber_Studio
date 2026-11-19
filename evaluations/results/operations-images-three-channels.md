# 三渠道运营配图隔离验收

2026-11-19。使用临时 SQLite、合成账号和 Playwright 驱动的 Edge。业务页面通过回环地址打开，平台适配器的 HTTP 请求由进程内 MockTransport 接收。正式平台写入为零。

| 运营入口 | 核对的对象和正文 | 独立回执 |
| --- | --- | --- |
| 评论处理 | 从待审楼层回复选图库图，审核正文。捕获的表单保留原视频资源、楼根、直接父评和 pictures。视频主评另测 root=0、parent=0。 | 图片上传后提交一条评论，取得 rpid。 |
| 私信 | 在收到来信的会话中选择「附图并保留正文」。图片和正文均发往审核的同一 UID。旧的单表情模式仍只提交一条图片。 | 一次图片上传，type=2 和 type=1 各有一个不同的 msg_key。 |
| 动态 | 从保留的公开视频素材保存动态，隔离模型决定配图，图片服务返回 PNG。浏览器从认证的草稿预览地址实际解码 1024 × 1024 图片，审核后提交相同 PNG 字节和正文。 | 一次图片上传，scene=2 创建动态，取得 dynamic_id。 |

从「自主运营」进入三个页签，共捕获三次上传和四次最终平台请求。另从号主活动入口运行动态委派审批，限定的一条动态取得回执，另一条保留待审，服务和浏览器的外部连接均为零。

生成路径单独验证了评论生成队列、自主浏览主评、自动读取来信、从已读视频后台评估动态。模型选择纯文字时提交体保留原正文。公开评论的配图材料只引用本视频选中的证据，私信使用绑定会话的来信，动态使用选入草稿的公开素材。后台视频路径复用已读片段，无需 Realtime。

故障用例覆盖图片送达后撤销私信渠道授权，以及正文提交后传输断线。前者只捕获一条图片消息，后者捕获图片和正文两条请求。两者均保存图片消息 ID，分别标记正文未提交或结果不确定。再次调用发送不会产生新的上传或消息。两步缺少可核对的原消息 ID 时保留待核对。

## 上游接口证据

核对发行包中的 `comment.py::send_comment`、`session.py::send_msg` 和 `dynamic.py::upload_image/send_dynamic`。评论端点使用 JSON 编码的 pictures 表单，每图包含 img_src、img_width、img_height 和 img_size。上传使用 file_up、biz=new_dyn 和 category=daily。私信分图、文两个消息类型。该 SDK 提供协议实现证据，正式账号发送权限、平台公开可见性及 GIF 播放效果均未实测。[bilibili-api-python 17.4.2 发行包](https://pypi.org/project/bilibili-api-python/17.4.2/)

## 复验入口

- `uv run --locked pytest -q tests/browser/operation_images_acceptance.py` 验证三渠道业务页面、图片预览和实际适配器请求。此场景使用本机 Edge。
- `uv run --locked pytest -q tests/test_operation_attachments.py tests/test_message_memes.py tests/test_bilibili_browsing.py` 验证后台入队、旧稿、撤权、部分送达和楼层表单。
- `tests/browser/owner_activity_fixture.py` 验证号主活动中的限定动态审批。可通过 CHROMIUM_EXECUTABLE 指向已有 Chromium 浏览器。

副本演练可检查新增的 operation_image_uploads、dm_delivery_steps、dm_attempts.image_submission，以及 drafts、dm_drafts 和 dynamic_posts 的 image_ref。浏览评论沿用 browsing_intents.target 绑定图片。既有纯文字记录的新增字段为空，不会因启动服务而补排配图。模型配图作业和生成工件由本地提交 97a992d 提供。
