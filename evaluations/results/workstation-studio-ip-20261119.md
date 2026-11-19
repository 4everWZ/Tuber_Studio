# 工作站 IP 部署与笔电浏览器验证

工坊的 Core、前端静态资源和业务卷已部署到推理和 Avatar 所在的工作站。HTTPS 代理仅在工作站私网 IP 上发布 80 和 443。直接打开 HTTP IP 会收到一次 308 跳转，HTTPS 健康检查返回 200。Core、TLS 代理、网关和 Avatar 均配置容器重启策略。

| 验证对象 | 从笔电到工作站的实测 |
| --- | --- |
| 页面与身份 | Windows Edge 校验 IP 服务器证书成功。访问正式入口，使用迁移保留的管理令牌从登录表单进入对话首页，登录 Cookie 带 Secure 和 HttpOnly。私钥保存在工作站。 |
| 私有语音 | 从「更多对话操作」进入临时语音通话。浏览器收到真实网关的 `session.created` 与 `session.updated`，页面进入麦克风已开启状态，随后正常挂断。本项使用测试麦克风设备，未采集环境声。 |
| 媒体资产 | 从正式 Core 使用同一普通应用凭据向工作站网关查询。Q、mature 两张获选成图及两套原稿的四个独立登记均返回 `inochi2d / ready`。内置 sprite 登记也在更新前后保留。实际连续播放与音画表现见单独媒体验收。 |
| 业务归属 | 正式 Core 以 `tuber-core` 身份持有迁移卷，笔电原库已退役。网关和媒体更新前查询到活动聊天、实时会话及排队数均为零，只滚动替换这两个服务。 |

读库迁移与历史缺文件详见[业务切换记录](workstation-business-cutover-20261119.md)。工作站原画发送端分片和浏览器缓冲的对照见[媒体传输记录](workstation-whep-ip-20261119.md)，获选成图的绑定见[两套 OC 姿态验证](approved-oc-rigs-20261119.md)。[工作站 Compose 配置](../../deploy/omni/workstation.compose.yml) · [独立服务接入](../../docs/specs/foundation/omni-deployment.md)
