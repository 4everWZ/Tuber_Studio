# 创建新OC

角色、音色、素材是三个独立引用，评论和直播流程共用这些接口；更换OC不需要修改调度器或模型Provider。

1. 在「角色」选一张卡，复制为新的稳定ID（如 `sea_salt`），输入名字。
2. 在JSON编辑器修改 `core`（身份、人格、示例对白）、`style`（口吻／字数）、`rules` 与 `lore`。保存会产生新版本，选择版本后可激活或回滚。
3. 修改 `voice_profile` 指向 `config/voices.json` 中独立音色。新音色填写Provider、版本、权重版本、参考音频／文本、来源、使用约束和推理参数；GPT-SoVITS每个服务绑定正确权重，不能在另一个角色播报时偷偷改全局权重。
4. 将图片放 `assets/sea_salt/`，建立 `manifest.json`，再把角色的 `avatar_manifest` 改为 `assets/sea_salt/manifest.json`。复制时可先复用旧素材，换成原创素材后再切换清单。

最小素材条目：

```json
{
  "entries": [
    {"id": "idle", "tag": "idle", "path": "assets/sea_salt/idle.png", "source": "原创", "license_status": "owned", "character_id": "sea_salt"},
    {"id": "talk", "tag": "talking", "path": "assets/sea_salt/talk.gif", "source": "原创", "license_status": "owned", "character_id": "sea_salt"}
  ]
}
```

支持idle／talking／cheerful／angry／confused／shy／gift等标签，缺失时回退待机。音效条目可用WAV及准确 `text`，Mock事件类型sfx通过 `metadata.asset_id` 引用，不接受任意磁盘路径。

音色模板和素材清单属于项目配置，加入Git；大权重和试听缓存留在runtime。修改音色版本／参数或参考音频内容会改变缓存身份。角色固定版本与上下文隔离，激活新版本会停止旧队列；已有评论草稿保留生成时的角色版本，不被新OC覆盖。
