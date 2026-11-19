# 工作站阅读进程

统一部署时，在现有 Core、工作站 edge 和正式业务卷的 Compose 文件后追加 `-f deploy/omni/reading-worker.compose.yml`，用同一源码构建 `tuber-core` 和 `reading-worker`。Core 的默认 Dockerfile 目标提供工作台，`reading-worker` 目标安装项目锁定的 Playwright Chromium Headless Shell 及系统依赖。更新已有部署时，为新版 Core 指定新的 `TUBER_CORE_IMAGE` 标签。

Core 核验账号、任务授权和公开来源，匿名获取文档，通过只读挂载的 Unix socket 与 worker 交换页面操作和视口。worker 使用 UID/GID 65532、独立 PID 和 IPC、无网络、只读根目录以及私有临时空间。worker 只挂载 socket 卷；正式业务卷及密钥由 Core 持有。worker 冷启动健康检查检测 socket 是否接受连接，页面验收需实际打开来源、读回正文与截图。

浏览器启动时开启原生沙箱。worker 使用 Playwright 1.64.1 随附的 seccomp 配置，按 Chromium 自建用户命名空间所需的权限运行；保留 Docker 默认的 `SYS_CHROOT` 边界位供 zygote 在其子命名空间内执行 chroot。工作站实测 worker 进程有效 capabilities 为 0。Core 沿用自身 seccomp。升级 Playwright 时，重新核对其浏览器、配置文件与 UID 65532 的实页验收。 [上游配置原件](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json) · [Playwright 容器沙箱说明](https://playwright.dev/python/docs/docker)
