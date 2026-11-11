# Q 版大肥鱼 Live2D 接入

当前默认演出使用图片／GIF。已安装的可选模型移至本机 `runtime/inactive-live2d/whale`，刷新场景后使用现有 GIF。版本化交付包继续留存，重新启用时按下文安装到 `runtime/live2d/whale`。

直播适配版 `0.2.0-community` 使用氵六青的「DS 鲸鱼娘」。鲸鱼娘原作为上善无形，DeepSeek 女仆二创为 ZipZipPipe。直播包保留原模型、贴图和物理，在运行配置里映射待机、说话、开心、困惑和礼物，另加轻点头、偏头和微笑。角色模型依 CC BY-NC-SA 4.0 用于非商业演出。 [来源与署名](https://github.com/xkk6663/dsh-live2d-mao#-许可与致谢) · [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)

本机交付包在 `runtime/releases/dafeiyu-avatar/0.2.0-community`。将包内 `model` 目录交给下文的安装工具，工作台与 OBS 会从角色 `whale` 的运行目录加载它。 `mouth_gain` 可在 `avatar.json` 调整，本版用 2.4。OBS 的每帧口型取当前音频时刻的包络，工作台静音会停播并闭嘴。 [工件清单](../../assets/whale/community-live2d.json) · [OBS 实测](../../evaluations/results/live2d-community-obs-20261111.json)

## 自制立绘的工程文件

分层 PSD 用于建模，`.cmo3` 保留可编辑网格、变形器、参数和物理，`.can3` 保留动作时间轴。Cubism 导出的 `.moc3`、`.model3.json`、贴图、动作及物理数据组成运行包。预览 PNG 和 SVG 属于立绘源素材。保持原始分层稿与编辑文件独立于运行包。

Windows 从官方页面安装 Cubism Editor，并由使用者确认其协议。首次下载需要有效邮箱。选择 FREE 后仍可编辑和导出基础模型。为便于继续编辑，Q 版目标为单张不超过 2048 px 的纹理图集、不超过 100 个 ArtMesh、30 个运动参数、50 个变形器和 30 个 Parts。导入时选择兼容 5.2 及更早版本的混合模式，输出选择与当前 Web 适配器兼容的 Cubism 4.2 目标。 [Cubism Editor](https://www.live2d.com/en/cubism/download/editor/) · [FREE 限制](https://www.live2d.com/en/cubism/comparison/) · [导入 PSD](https://docs.live2d.com/en/cubism-editor-manual/psd-import/) · [导出说明](https://docs.live2d.com/en/cubism-editor-manual/export-moc3-motion3-files/)

若标题栏显示「未注册」且保存、另存为和导出没有弹窗，先从正常启动对话框选择「作为 FREE 版启动」。确认可保存后再继续做绑定。Q 版分层稿使用 Cubism 已实测的层序和嵌入 sRGB 配置。 [官方启动与许可说明](https://docs.live2d.com/en/cubism-editor-manual/cubism-editor-activation/) · [Windows 排查](https://help.live2d.com/en/cubismeditor/cubism-does-not-start-win/)

## 绑定和动作

| 立绘图层 | Cubism 绑定 | 验收动作 |
| --- | --- | --- |
| 脸部和颈部 | 脸部弯曲变形器以 `ParamAngleX`、`ParamAngleY` 和 `ParamAngleZ` 分别设定中心与两端关键形，脸部器官随父级移动。 | 小幅转头时眼、眉、嘴跟随，侧发和颈部不露缝。 |
| `Eye_White_L/R`、`Iris_L/R`、`Eye_Lash_L/R`、`Brow_L/R` | 双眼以 `ParamEyeLOpen` 和 `ParamEyeROpen` 的 0 与 1 做独立闭眼，虹膜以 `ParamEyeBallX/Y` 移动。眼白、虹膜和睫毛的闭合关键形分别调整。 | 独立眨眼和睁眼；闭眼时虹膜不外露，瞳孔不越出眼白。 |
| `Mouth_Interior`、`Mouth_Line` | 建 `ParamMouthOpenY` 0／0.5／1，闭嘴使用线稿，张嘴显露口内并对嘴角做渐变；嘴形 `ParamMouthForm` 控制笑与困惑。 | 实际语音播放时张合，停顿、暂停、静音、结束和断线时闭嘴。 |
| `Hair_Back_L/R`、`Hair_Side_L/R`、`Ear_Fin_L/R`、`Ahoge` | 头部为父级，两侧发、耳鳍、呆毛各有局部变形器。将头角度与呼吸作为轻微摆动输入，动作为物理输出留出余量。 | 呼吸与点头时末端跟随，回中位后形状一致。 |
| `Tail_Root`、`Tail_Shaft`、`Tail_Fluke` | 以尾根接缝为锚点，由根、柄到尾鳍建立父子变形器。用 `ParamTailSwing` 和 `ParamTailTip` 分配缓摆与延迟回弹。 | 根部与裙摆衔接稳定，尾柄与尾鳍连续，尾鳍不靠平移整条尾巴表现。 |

导入 PSD 后先保存一份 `.cmo3`，检查纹理图集再保存绑定版本。动画工作区保存 `.can3`。待机包含呼吸、眨眼和小幅尾鳍摆动，建议先做 4–6 秒可循环时间轴；说话动画只驱动身体和耳尾动作，口型由播放器控制。开心和困惑采用有限表情，另导出 `.exp3.json` 时保留已使用的模型参数 ID。将导出的 `.motion3.json` 放入 `.model3.json` 的对应 `Motions` 组，将表情放入 `Expressions`。同一场景的音频由 OBS 播放器输出，动作文件无需内嵌音效。 [Cubism 文件类型](https://docs.live2d.com/en/cubism-editor-manual/file-type-and-extension/)

## 安装到工作台

在运行目录的 `live2d/sdk/` 放入本机授权的 `live2dcubismcore.min.js`，配合 PixiJS 6 的浏览器版 `pixi.min.js` 和 pixi-live2d-display 0.4.0 的 `cubism4.min.js`。三者分别保存，不随角色导出包分发。将下方映射保存为导出目录的 `avatar.json`，按实际导出名修改。保留 Q 版项目的自用、非商业记录。 [Cubism SDK](https://www.live2d.com/en/sdk/download/web/) · [发布许可](https://www.live2d.com/en/sdk/license/) · [Pixi Cubism 适配器](https://github.com/guansss/pixi-live2d-display/tree/v0.4.0)

```json
{
  "model": "dafeiyu-q.model3.json",
  "license": "原创大肥鱼 Q 版，仅本机自用及非商业演出",
  "commercial_allowed": false,
  "mouth_parameter": "ParamMouthOpenY",
  "motions": {
    "idle": {"group": "Idle", "index": 0},
    "talk": {"group": "Talk", "index": 0}
  },
  "expressions": {
    "cheerful": "Cheerful",
    "confused": "Confused"
  }
}
```

在项目环境中运行以下两条命令。第一条检查导出目录、MOC3 文件标记、贴图、动作和 SDK，第二条安装通过检查的运行文件。已有角色目录时先移出备份。编辑源文件留在制作目录。

```powershell
python tools/deployment/install_live2d_avatar.py <导出目录> --runtime <运行目录>
python tools/deployment/install_live2d_avatar.py <导出目录> --runtime <运行目录> --install
```

在 Cubism Editor 重开 `.cmo3` 和 `.can3`，逐项检查绑定和时间轴。在工作台刷新场景，再到 OBS 使用「OBS 浏览器源」链接。用透明场景检查轮廓与背景，用一条本机 Mock 语音检查音画、自然停顿与播放回执。检查静音、停止、断线和第二个 Browser Source 的待机。缺少文件时验证 GIF 可接管。预检和 GIF 回退的 OBS 测试只能确认各自链路，实际模型装载、口型与动作分别记录。
