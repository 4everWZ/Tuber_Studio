# 大肥鱼视觉微调

`grounded_sft.py` 是 MiniCPM‑V‑4.7 新实验的入口。旧版 DoRA 及四组报告保留作为历史对照。

## 数据与工作站

- 底模为 `openbmb/MiniCPM-V-4.7-1B` 的 `ad9bf379feb71204c888b22923d63322452407f9` BF16 快照，须含 `preprocessor_config.json`、`processor_config.json` 与原 chat template。
- 已实跑的环境为 Python 3.12、torch 2.10.0+cu130、transformers 5.22.1、PEFT 0.19.1、accelerate 1.13.0。使用工作站项目专用环境，单卡 BF16。
- `draft_oc_dialogues.py` 从固定版本的原创中文角色扮演语料提出两轮大肥鱼对白，原场景先确定划分。`review_oc_dialogues.py` 用独立模型评分、限重复开头并筛重。保留审核标记和两级模型版本。可用人工审核的同格式记录替换机审输出。
- `grounded_video_annotations.json` 指定三份授权动画。训练用 Caminandes 和 Sintel，整部 Big Buck Bunny 预告留给训练验证。数据目录中的文件名按标注表。FFmpeg 按人工复核的序列取帧，保留解码时码与授权信息。B 站样本只用于冻结回放。

在已安装本项目的环境组装语料，再把输出目录传到训练工作站。

```bash
python tools/training/build_grounded_corpus.py --media "$MEDIA" --approved "$OC_APPROVED" --output runtime/grounded-corpus
```

正式训练的输入固定为 600／60 条。两组共享来源、划分、种子和超参。文本组把实际图像替换成与之对应的人工观察。验证损失只在各组内部选择 checkpoint。

```bash
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus --output runtime/sft-vision-v1 --strategy vision
CUDA_VISIBLE_DEVICES=1 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus --output runtime/sft-text-v1 --strategy text
```

`--resume PATH` 从 Trainer checkpoint 恢复。`--pilot` 用独立输出和小语料验证训练链路。训练日志、实际依赖与逐 epoch 验证结果写入各自 `run.json`。

第二轮保留首轮语料，为同一问题增加图文齐全、人工画面观察代替实帧、缺字幕和缺画面四种输入。训练为 760 条，验证为 76 条。`--init-adapter` 从既有权重开始新一轮优化，优化器和学习率重新初始化。

```bash
python tools/training/augment_grounded_corpus.py --original runtime/grounded-corpus --output runtime/grounded-corpus-v2
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-v2 --output runtime/sft-v2-continue --strategy vision --curriculum mixed-evidence --init-adapter runtime/sft-vision-v1/adapter --learning-rate 1.8e-5 --epochs 1
CUDA_VISIBLE_DEVICES=1 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-v2 --output runtime/sft-v2-fresh --strategy vision --curriculum mixed-evidence --learning-rate 3e-5 --epochs 2
```

角色轮保留第二轮训练数据，补 104 条训练记录和 20 条验证记录。原创多轮对白同时用纯文本和直播台词／情绪的 JSON 协议训练，视频反应同时用真帧台词和带来源引用的 JSON。`--roleplay-repeat` 只调整训练采样，原始对话和影片仍按同一划分留出。

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v3 --characters config/characters.json
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v3 --output runtime/sft-v3-roleplay --strategy vision --curriculum roleplay --init-adapter runtime/sft-v2-continue/adapter --learning-rate 2e-5 --epochs 1
CUDA_VISIBLE_DEVICES=1 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v3 --output runtime/sft-v3-weighted --strategy vision --curriculum roleplay --roleplay-repeat 4 --init-adapter runtime/sft-v2-continue/adapter --learning-rate 5e-5 --epochs 1
```

`--chatanime` 加入固定来源的 32 个处境改写。每个场景保留来源 ID，8 个验证场景整组留出。ChatAnime 原角色的回答和 DeepSeek 网页对照原答与训练语料分别保存。完整训练集为 936 条，验证集为 120 条。[场景改写](grounded_chatanime_dialogues.txt) · [ChatAnime 原始数据](https://github.com/LanlanQiu/ChatAnime/blob/2169710041b1130b79530be610c0adef886f96fb/data/human.json)

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v4 --characters config/characters.json --chatanime
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v4 --output runtime/sft-v4-anime --strategy vision --curriculum roleplay-anime --init-adapter runtime/sft-v2-continue/adapter --learning-rate 2.5e-5 --epochs 1
```

`--deepthink` 在上述语料上加入逐轮复核的 30 个两轮对白，24 个用于训练，6 个整组留出。数字 ID 沿用 ChatAnime，命名 ID 对应项目自编场景。纸月亮保留原答，展台灯先问有没有拨轮。完整语料为 1008 条训练、138 条验证。用第二轮适配器继续训练一轮，角色样本每轮采样一次。[最终对白](grounded_deepthink_dialogues.txt) · [网页草稿与逐例回放](../../evaluations/results/grounded-persona-v5-20261105/README.md)

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v5 --characters config/characters.json --deepthink
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v5 --output runtime/sft-v5-deepthink --strategy vision --curriculum roleplay-deepthink --init-adapter runtime/sft-v2-continue/adapter --learning-rate 1.8e-5 --epochs 1
```

`--paired-context` 复用 V5 的对白、V2 的真实帧和视频目标。另补 14 组 ChatAnime 改写，以及 12 对训练、4 对验证的前文对照。每对有两个不同前文，第二轮观众原话相同。14 组改写里有 9 个新源场景，另 5 组取同一训练源场景的不同后续轮次，来源组保持一致。完整语料为 1066 条训练、146 条验证。

`--current-live-turn-only` 只对有 `format: live_reply` 标记的记录排除历史助手台词的损失。历史文本仍进入模型；本轮 JSON 和结束符照常算损失。两组均以 V2 适配器为起点，沿用相同语料、底模和超参。[前文对照](grounded_paired_dialogues.txt) · [ChatAnime 新对白](grounded_chatanime_followups.txt) · [实验与逐例结果](../../evaluations/results/grounded-persona-v6-20261105/README.md)

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v6 --characters config/characters.json --paired-context
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6 --output runtime/sft-v6-all --strategy vision --curriculum roleplay-paired --init-adapter runtime/sft-v2-continue/adapter --learning-rate 1.8e-5 --epochs 1
CUDA_VISIBLE_DEVICES=1 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6 --output runtime/sft-v6-current --strategy vision --curriculum roleplay-paired --init-adapter runtime/sft-v2-continue/adapter --learning-rate 1.8e-5 --epochs 1 --current-live-turn-only
```

`--paired-context curated` 从训练中移除 V2 的 240 条早期 OC 草稿，保留 826 条训练、146 条验证。用相同设置比较 V2 续训、从底模新训 LoRA、从底模新训 DoRA。DoRA 对同样的语言层投影增加幅值参数，视觉塔和投影器保持冻结。[PEFT DoRA](https://huggingface.co/docs/peft/package_reference/lora_variant_dora)

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v6-curated --characters config/characters.json --paired-context curated
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6-curated --output runtime/sft-v6-curated --strategy vision --curriculum roleplay-paired-curated --init-adapter runtime/sft-v2-continue/adapter --learning-rate 1.8e-5 --epochs 1 --current-live-turn-only
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6-curated --output runtime/sft-v6-fresh --strategy vision --curriculum roleplay-paired-curated --learning-rate 1.8e-5 --epochs 1 --current-live-turn-only
CUDA_VISIBLE_DEVICES=1 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6-curated --output runtime/sft-v6-dora --strategy vision --curriculum roleplay-paired-curated --learning-rate 1.8e-5 --epochs 1 --current-live-turn-only --use-dora
```

`--paired-context teacher` 再加 6 组来自网页开关对照的两轮对白，以及 8 个 ChatAnime 场景的第三轮。第三轮继承原来源组、前两轮和数据划分，只生成一条直播训练记录。语料为 850 条训练、148 条验证。第七轮从视频表现最好的 V6 适配器继续训练一轮。[两轮对白](grounded_teacher_dialogues.txt) · [第三轮](grounded_chatanime_continuations.txt) · [第七轮回放](../../evaluations/results/grounded-persona-v7-20261105/README.md)

```bash
python tools/training/augment_grounded_persona.py --original runtime/grounded-corpus-v2 --output runtime/grounded-corpus-oc-v6-teacher --characters config/characters.json --paired-context teacher
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-corpus-oc-v6-teacher --output runtime/sft-v7-teacher --strategy vision --curriculum roleplay-paired-teacher --init-adapter runtime/sft-v6-all/adapter --learning-rate 1.2e-5 --epochs 1 --current-live-turn-only
```

## 回放与导出

- `evaluate_grounded_sft.py` 在工作站运行 24 个 OC 用例和保留画面。多轮题把真实上轮回复放回历史。
- `prepare_grounded_replay.py` 将固定的 40 个 B 站问题绑定到公开视频字幕缓存与 `BrowsingAdapter.video_frames()` 取得的真实故事板。保留原 BVID、CID、时间和短引用映射。
- `replay_grounded_endpoint.py` 以原生 llama.cpp 结构约束回放同一素材。逐例审查事实、引用和正确暂缓。工作站回放与 Jetson 生产入口的延迟分别记录。
- 新视频回放使用 `evaluations/cases/grounded-real-video-v2.jsonl`。`--evidence-first` 在同一个请求内按字幕、带短引用的画面、观众问题的顺序输入。`--source-notes` 是加入逐源提取字段的探索对照。原始输入是默认对照。`evaluate_grounded_sft.py --oc-only` 单独复查 24 个 OC 场景。
- `replay_grounded_live.py` 对 `evaluations/cases/grounded-persona-v3-oc.jsonl` 使用实际直播提示组装、推理 Provider 与 JSON 校验，每轮回填模型真实台词。`grounded-persona-v3-video.jsonl` 用原来的公开素材换成接梗和解说题，继续用 `prepare_grounded_replay.py` 和 `replay_grounded_endpoint.py` 回放。

```bash
python tools/training/export_grounded_sft.py --base "$BASE" --adapter "$ADAPTER" --llama-cpp "$LLAMA_CPP" --output "$CANDIDATE_DIR"
```

导出包含 Q4_K_M、4x `mmproj` 和带版本及 SHA256 的 `bundle.json`。只在独立路径启动候选，候选使用专有别名 `MiniCPM-V-4.7-1B-Dafeiyu-Grounded`。切换前记录正在使用的运营模型、视觉证据配置和直播模型。回滚时恢复这三项及原来的准入证明，停止候选进程即可。候选隔离验收使用独立业务数据库和预览入口。

## 大肥鱼第一人称重训

本节两套语料用于工作站实验。线上运营权重继续使用 V6。

用已部署 V6 的语料做对照基准。新版去掉 240 条早期机审草稿，加入 118 条大肥鱼第一人称样本，含 38 组两轮对话的普通和原生格式，以及 4 条动态。另用 12 条多角色二次元对白替换等量旧 OC 记录。六个原创角色各有独立短卡，用来练习切换角色时保留各自的偏好和处境。原视频、通用、运营协议样本及验证集沿用 V6，训练共 944 条。等量对照保留随机抽取的旧草稿。两组使用同一个固定底模、LoRA 和一轮预算。[大肥鱼对白与动态](grounded_dafeiyu_first_person.txt) · [多角色对白](grounded_anime_transfer_dialogues.txt)

人设以项目的大肥鱼角色卡和操作者补充的外观、鲸尾行为为准。社区二创提供白饭、嘴硬和认真时肯帮忙的参考。六个 ChatAnime 改写沿用原场景分组，QQ 版 `gj-persona-03-e196985e` 提供说话节奏和尾巴互动的参考，台词重新写。QQ 的消息长度限制、工具和权限配置仍归 QQ Bot。训练用短身份，按场景写第一人称的决定、反应或改口。[社区角色设定](https://github.com/YunYueSama/codex-deepseek-pet/blob/main/docs/%E8%A7%92%E8%89%B2%E8%AE%BE%E5%AE%9A.md) · [ChatAnime](https://github.com/LanlanQiu/ChatAnime/tree/2169710041b1130b79530be610c0adef886f96fb)

```bash
python tools/training/build_dafeiyu_first_person.py --original runtime/grounded-corpus-oc-v6 --output runtime/grounded-first-person --characters config/characters.json
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/grounded-first-person --output runtime/sft-first-person --strategy vision --curriculum dafeiyu-first-person --learning-rate 5e-5 --epochs 1
```

语料构建加 `--control` 生成等量旧稿对照。回放重点是她是否记得自己的偏好、有没有听见上一句、能否自然接梗以及有没有乱揽别人的事。用 `replay_anime_plain.py` 直接回台词，检查同一权重在短身份、精简卡和其他角色短卡下的实际输出。通用问答与视觉单独回归。[直播题](../../evaluations/cases/grounded-first-person-live.jsonl) · [鲸尾题](../../evaluations/cases/grounded-first-person-tail.jsonl) · [运营题](../../evaluations/cases/grounded-first-person-operations.jsonl) · [多角色留出题](../../evaluations/cases/grounded-anime-transfer.jsonl)

### 经典角色与大肥鱼变体混合

从固定 ChatAnime 人写子集选 16 个经典角色，每个 14 组两轮对白，共 224 组。每条明确给角色短卡。按来源场景分组，先选训练组，再筛掉篇幅过长和明显模板化的对白。四个完全不参与微调的角色各留四组，取自训练阶段未见过的来源场景。[固定人写数据](https://github.com/LanlanQiu/ChatAnime/blob/2169710041b1130b79530be610c0adef886f96fb/data/human.json)

大肥鱼保留 118 条核心样本，另用 18 组两轮对白生成 54 条变体记录。电台、开黑和日常三张短卡只提供情境与节奏，样本中的身份、偏好、鲸尾行为与相处边界保持一致。另留 12 条原创角色样本。能力保留使用原 60 条通用题、60 条运营协议及分层抽取的 240 条视频记录，其中 120 条含实帧。合计 768 条，从同一原始底模训练一轮，48 步。[变体对白](grounded_dafeiyu_variants.txt) · [同题短卡回放](../../evaluations/cases/grounded-dafeiyu-variants.jsonl) · [基础能力回放](../../evaluations/cases/grounded-anime-mix-retention.jsonl)

```bash
python tools/training/build_anime_dafeiyu_mix.py --original runtime/grounded-first-person --output runtime/anime-dafeiyu-mix --characters config/characters.json --chatanime-human "$CHATANIME_HUMAN_JSON" --holdout runtime/classic-holdout.jsonl
CUDA_VISIBLE_DEVICES=0 python tools/training/grounded_sft.py --base "$BASE" --data runtime/anime-dafeiyu-mix --output runtime/sft-anime-dafeiyu-mix --strategy vision --curriculum anime-dafeiyu-mix --learning-rate 3e-5 --epochs 1
```

## 有上限的偏好实验

`train_grounded_preference.py` 使用冻结的 V6 合并 BF16 作为参考，每步读三条正文偏好记录和一条保留记录，共 32 步。偏好只比较正文，保留条目沿用原协议和真实帧。用 `--reference-model` 将训练出的增量合回相同参考模型。运营回放保留正常请求预算和完整原文，单独看回复是否完成及是否符合来源与角色。[实验记录](../../evaluations/results/grounded-preference-20261105/README.md)

```bash
python tools/training/build_grounded_preferences.py --corpus "$TEACHER_CORPUS" --characters config/characters.json --output runtime/grounded-preferences.json
CUDA_VISIBLE_DEVICES=0 python tools/training/train_grounded_preference.py --base "$BASE" --reference "$V6_MERGED" --corpus "$TEACHER_CORPUS" --data runtime/grounded-preferences.json --output runtime/sft-next-preference
python tools/training/export_grounded_sft.py --base "$BASE" --reference-model "$V6_MERGED" --adapter runtime/sft-next-preference/step-32 --llama-cpp "$LLAMA_CPP" --output runtime/gguf-next-preference-32
```
