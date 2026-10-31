# 可选在线点歌组件

只增加一个 Node.js 22+ 辅助进程，通过私有 stdio JSON-lines 与 Python 通信；不启动网易云 HTTP 服务、UNM 代理或模型。直接执行 `npm start` 也可独立收发协议。普通安装及主镜像不安装 Node／npm 依赖，默认开关关闭。

在本目录执行 `npm ci --ignore-scripts`，然后在 `/live` 的「在线点歌」中启用。依赖及其传递版本由 `package-lock.json` 锁定，使用指定仓库的固定提交：

Docker部署可使用CI发布的在线环境扩展：首次运行启动器时传`--environment online`，以后按数据目录保存的环境选择继续复用。程序包携带base／online两种固定环境引用；更新helper源码不重建镜像，npm依赖改变才更新扩展。工作台播放用途可显式选择“仅本机／OBS技术测试”，不代表确认直播用途，只接收工作台／Mock请求。

- [xgxdmx/NeteaseMusic-API](https://github.com/xgxdmx/NeteaseMusic-API/tree/0395d087d9e99ceef480c54e7932c2332e5c0c39)，MIT，仅使用 `cloudsearch`、`song_detail`、`lyric`／`lyric_new`，不调用其 `song_url_v1` 或 `unblockmusic-utils` 解析。
- [UnblockNeteaseMusic/server](https://github.com/UnblockNeteaseMusic/server/tree/1d64281c0fae81a4e3cf21f176706b0baa3a606d)，LGPL-3.0-only，仅调用波点／咪咕 Provider；保留其包内许可。通过模块边界替换宽松的 `select`，拒绝首条结果兜底；禁用 `insure` 远程解析兜底。

元数据请求有 8 秒超时，音源请求沿用上游的有界超时；Python RPC、下载、容量和时长另有限制。辅助进程的 stdout 专用于协议，不把上游日志或音源签名地址发往工作台。网易云匿名 token 使用本服务 runtime 内独立临时目录，不读共享系统临时目录的登录 token。

咪咕需要在启动 Python 服务前设置 `MIGU_COOKIE` 环境变量，辅助进程继承它。界面仅显示是否配置，不回显凭证。没有凭证时波点仍可使用，咪咕尝试明确失败。更新依赖后重启点歌组件。

默认配置保存在现有数据库 KV `song_requests`，由 WebUI 修改：

```json
{
  "enabled": false,
  "sources": ["bodian", "migu"],
  "quality": "flac_preferred",
  "fallback_bitrate": 320000,
  "allow_lower_quality": false,
  "cache_limit_mib": 15360,
  "max_download_mib": 96,
  "max_duration_seconds": 600
}
```

按音源优先级尝试：波点 FLAC → 波点高码率 → 咪咕 FLAC → 咪咕高码率。`ENABLE_FLAC`、`FOLLOW_SOURCE_ORDER` 为 true，完全删除 `SELECT_MAX_BR`，不能写字符串 false。实际流若是满足阈值的 MP3，也可作为 FLAC 请求的回退结果；低码率默认拒绝。FLAC 是检测到的流格式，不代表源录音未经历过有损编码。

跨源严格核对标题、所有歌手及版本标记；有候选时长时要求差值不超过 3 秒。咪咕候选常缺时长，因此必须在完整下载后再检查实际流时长。实际格式、采样率、位深／码率由 Python Mutagen 读取，播放确认由浏览器解码及现有 started／ended ACK 完成。缓存保存原始 FLAC／MP3，不转码、不保存过期签名 URL。

音频只从支持的公开音乐 CDN 下载，每次跳转都验证主机和公网 DNS，并固定连接到已检查的地址；不接受观众提供 URL／路径。新增 CDN 须显式维护 Python 的下载白名单。每首下载最多 90 秒、默认 96 MiB／600 秒；只预备当前及下一首歌曲，按共享音乐池上限（最高15 GiB）淘汰非占用缓存，不能删除当前播放、下一首或本地曲库引用的文件。

关闭或重新配置／重启组件会取消在线待播、准备及当前在线歌曲，并退出旧辅助进程。配置、缓存、本地歌曲和 TTS 保留。组件异常不自动无限重启，显示原因并提供重启操作。在线音源沿用项目的用途许可检查，在设置中记录目标使用范围；检索成功不替代用途确认。

请求格式为一行 JSON，例如：

```json
{"id":1,"method":"ping"}
{"id":2,"method":"search","params":{"query":"歌名 歌手"}}
{"id":3,"method":"detail","params":{"id":"123"}}
{"id":4,"method":"lyrics","params":{"id":"123"}}
{"id":5,"method":"resolve","params":{"id":"123","source":"bodian","flac":true}}
```

响应包含同一 `id` 及 `result` 或 `error`。`resolve` 的临时 URL 仅供 Python 下载使用，不是可直接提交至播放器的公共接口。解析串行，元数据与健康查询可并发。

可选容器变体：先构建匹配依赖的环境镜像，再执行：

```sh
docker build -f components/song_requests/Dockerfile --build-arg TUBER_IMAGE=tuber-studio:environment-r36.4 -t tuber-studio:online .
```

该环境变体携带 Node 与 npm 依赖，辅助进程源码仍来自只读程序挂载，通过 NODE_PATH 复用镜像内依赖；启动预检核对变体内的 npm 锁文件，依赖不匹配先报错。只有 npm 依赖变化才重建该变体，helper 源码变化直接随程序更新；继续使用同一启动器、runtime 卷及非 root 用户。普通 CI 另验证组件安装、匹配规则和真实 stdio 进程，不请求外部歌曲；各音源可用性及 OBS 本体解码仍需在目标网络／设备验收。

当前Jetson已安装并启用该扩展，保存online环境选择；本机测试的真实搜索、波点FLAC和浏览器播放证据见[设备验收](../../evaluations/results/online-song-component-device-20261031.json)。新安装仍默认关闭；咪咕需单独配置凭证，浏览器结果不代替OBS本体验证。
