# 星露谷隔离实机

工坊与游戏通过同机的任务目录交换状态和动作。工坊进程只需要访问通信目录。游戏端使用 SMAPI，仓库只分发桥接源码。本机验证组合为 Stardew Valley 1.6.15、SMAPI 4.3.2、WSL2 Linux x64 和 .NET 6。

## 准备任务目录

1. 在空的任务目录下创建 `game/`，把已授权安装的游戏完整复制进去。在副本中创建空标记 `game/.tuber-task-copy`。保留原安装和 Steam 启动选项。
2. 按 SMAPI 官方手工安装说明将安装包中的 `internal/unix/install.dat` 解压到副本，将副本中的 `Stardew Valley.deps.json` 复制为 `StardewModdingAPI.deps.json`。[官方手工安装说明](https://github.com/Pathoschild/SMAPI/blob/develop/src/SMAPI.Installer/assets/README.txt)
3. 在任务目录的 `dotnet/` 解压 Linux x64 .NET 6 runtime。复用 Windows 游戏副本时，将该 runtime 的 `shared/Microsoft.NETCore.App/6.0.x/*.dll` 复制到任务的 `game/`，使框架程序集与 Linux 运行时一致。本次使用 6.0.36。副本启动前可在 `game/smapi-internal/config.user.json` 设置 `{"CheckForUpdates":false}`。
4. 在有 .NET SDK 的开发环境编译 `dotnet build integrations/stardew/IsolatedFarm.csproj -c Release -p:GamePath=/absolute/path/to/task/game`。将输出的 `IsolatedFarm.dll` 和 `manifest.json` 放入任务目录的 `studio-mods/IsolatedFarm/`。

## 运行

在仓库根目录运行下列命令。启动器为游戏指定独立的 XDG 配置、数据和模组目录。游戏进入标题后，可在游戏内选择任务存档。

```powershell
wsl -d Ubuntu-24.04 -- bash integrations/stardew/run-isolated.sh tmp/stardew-device
```

在启动工坊的 PowerShell 中设置 `$env:TUBER_STARDEW_BRIDGE = (Resolve-Path tmp/stardew-device/bridge).Path`，再按原方式启动工坊。在「直播与游戏 → 互动与游戏 → 星露谷物语 · 隔离实机」查看存档和控制权。正常游戏保存遵循游戏自己的保存时机。

- 默认由号主控制。交回后可转身、切换背包格，或用当前工具／种子作用于正前方农场地块。页面接管按钮和游戏窗口 F8 都会立即更新控制版本。连续 30 秒无工坊动作后自动归还控制。
- `GET /api/games/device/stardew` 返回最近快照及在线标志。`POST /api/games/device/stardew/actions` 接受会话 UUID、动作 UUID、控制版本和动作；农活另绑定地块与手持物品。`GET /api/games/device/stardew/actions/{session}/{id}` 可查询原尝试。游戏在后续 tick 核对结果，断开后用原编号查询。
- 配置了通信目录的工坊会注册 `stardew_status` 与 `stardew_action`。Agent 执行前需要本轮对应实机观察及号主对该动作的批准。授予工坊控制权只能由号主发起。
