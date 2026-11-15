# 2026-11-15 号主界面测试 fixture 修复

- 基线：`main` / `24261b120f00af3aedd6a409a2346204b160ff2a`。编辑前确认没有并行修复 PR。
- [Actions #80 / run 43677260837](https://github.com/4everWZ/Tuber_Studio/actions/runs/43677260837)：CPU job `132700019604`，`Unit and CPU graph contracts` 步骤失败；2579 passed，2 failed。
- 失败均由 `tests/test_account_ui.py::test_shared_web_clients` 调用 Node suite：`web_character_studio.test.cjs` 缺少 `avatar.querySelector`，`web_workbench_state.test.cjs` 缺少 `image.removeAttribute`。两者均在真实 `renderOwner` 中抛出 TypeError；本地原样复现。
- 独立 Jetson program package verification 已在失败基线通过；发布步骤被 CPU gate 跳过，与打包内容无关。

修复仅涉及测试：补齐局部头像图片查询、属性读取/设置/删除与 `src` / `href` 属性反映；继续调用真实 owner renderer。保留原来的页签/人设保留和过期角色请求测试。新增通过 UI 查询号主、写入已核验资料、清空绑定后移除头像与链接的回归，并验证旧角色响应不能覆盖当前号主，切到未绑定角色会清掉旧头像与链接。应用逻辑、CI gate、格式策略、打包与镜像逻辑均未改动。

本地验收（Node v24.22.1，Python 3.12.14，锁定项目依赖）：

- `node --test tests/web_character_studio.test.cjs tests/web_workbench_state.test.cjs`：18 passed，0 skipped。
- `node --test tests/*.test.cjs`：65 passed，0 skipped。
- `uv run --locked --offline pytest -q tests/test_account_ui.py tests/test_character_oc_types.py`：22 passed，1 条既有 Starlette 弃用警告。执行前仅对该进程移除沙箱注入的代理环境变量，以匹配 GitHub runner。
- `git diff --check`：通过。

PR 使用独立修复分支；远程完整 CPU、歌曲组件和打包检查以该 PR 的 Actions run 为准。未执行合并、部署、release dispatch、真实平台提交或镜像重建。
