# 2026-11-14 CPU CI 修复验收

## 失败基线

- 分支：`main`；提交：`4be18b8431cd701ef41f9a96f07a892705a0bdaf`。
- [Actions run 43573482721](https://github.com/4everWZ/Tuber_Studio/actions/runs/43573482721)，job `132382619779`（Python and CPU contracts），步骤 `Unit and CPU graph contracts`。
- 日志结果：2 failed, 2530 passed。失败为：
  - `tests/test_browsing.py::test_discovery_defers_failed_attempt_then_continues_other_videos[1]`：`retry_wait` / `recovered`。
  - `tests/test_dynamics.py::test_automatic_dynamic_checks_actual_caption_against_public_thread[supported]`：`pending_review` / `published`。
- 先前 PR #8 合并后的 [run 43432717479](https://github.com/4everWZ/Tuber_Studio/actions/runs/43432717479) 成功，不是本次失败。此次两个 ARM64 环境 job 均成功复用已有镜像；程序打包被 CPU 测试失败跳过。

## 根因与修复

1. 动态用例虽替换平台发送函数，仍真实渲染原评论卡，并在构造发送内容哈希时调用字体加载。CPU runner 只安装 ffmpeg；中文字体缺失时 `_font` 抛出 `ValueError`，`evaluate` 保留待审。临时探针将字体路径设为不存在并清空字体/图片缓存，稳定复现 `pending_review`。CI 安装 `fonts-noto-cjk`，继续保留真实图片渲染，不改动事实核对、授权与发送检查。
2. 浏览用例把适配器记录发送当成完整任务结束。普通停止会启动异步 drain 并立即返回；恢复状态在后续视频任务 checkpoint 才保存。临时探针在已确认发送后、最终 checkpoint 前阻塞发布返回，稳定复现 `retry_wait`。回归用例用 Event 保留这个边界，停止后释放并有界等待 drain，再读取恢复状态与失败次数；不增加定时等待、不改运行时停止语义。

## 本地验收

- Python 3.12，锁定项目依赖，图检查依赖与 CI 相同：`numpy==2.2.6`、`onnx==1.17.0`、`PyYAML==6.0.2` 及 `tools/deployment/business-requirements.txt`。
- 完整 CPU 命令：2532 passed，1 条现有 Starlette 弃用警告，157.59 秒。
- 定向浏览/动态和停止回归：22 passed；原先强制 post-send 竞态探针修复后 1 passed。
- `node --test tests/*.test.cjs`：63 passed。
- 程序上下文生成、`audit_program.py`、独立 `package_program.py` 均成功；生成清单，不下载模型；环境 identity 仍为 `8468c1d68f33aee0ec15915e19df8cbeafd48fa23453ae831a21e8f9b3b012e1`。
- `git diff --check` 成功。CI 中测试继续阻断；格式检查现状不变。
- 沙箱注入的 SOCKS 代理让最初完整本地运行在 httpx 构造时报缺少可选 socksio；本地验收对进程移除代理环境变量，并使用已缓存依赖离线运行 uv，以匹配 GitHub runner。未修改项目依赖。

未执行真实平台发布、合并、部署、发版或 ARM64 镜像重建。GitHub 新提交上的 Actions 结果以对应 PR 的 run 为准。
