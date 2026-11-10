# 2026-11-11 原生 Instruct 模型开发对照

## 条件和材料

使用 Workstation 上隔离的 llama.cpp CPU `b12248`（`3837bf0bc`），8192 上下文、单槽、10 线程，关闭思考和采样，seed 42，每轮最多 128 tokens。加载官方 GGUF 和配套视觉投影器，使用未加适配器的量化模型。相同输入共五例、六轮，含一次跨轮更新、一次同名 UID、一次无图对照和两张实采 JPEG。输入 SHA256 为 `a310cf5d3dcf905005c0b0f70365a60bdfb8f48cb37f152390df08b921a6c861`。[对话夹具](../cases/grounded-operations-transfer.jsonl) · [UID 夹具](../../tests/test_browsing_identity_boundary.py) · [原图清单](operations-vlm-20261102-performance/visual-inputs.json)

这些是已有开发材料，和 172 项冻结发布评测分开。跨轮更新夹具只给一段纠正陈述，未明说要复述当前归属，评审保留这一限制。CPU 回放用于检查实际能力，不提供 AGX 资源或时延结论。

## Gemma 4 12B-it

记录 `dafeiyu-native-dev-f3w_7se4`，官方量化仓库修订 `790d826c6ec7dc8aa0ab23af33e5b8313140a231`。Q4_0 主权重 SHA256 `3712b9bd32cae83a22f67ee7a4466d8d7a4f21646ac8a07d19bf9418e8767a70`，Q8_0 projector SHA256 `6b1088334bda27d7f293dcf426c0efaf1f1f20b65415ea354b695dbed9a0d65d`。两件均校验完成，官方直连传输含校验耗时 262.5 秒。[官方工件](https://huggingface.co/ggml-org/gemma-4-12B-it-GGUF/tree/790d826c6ec7dc8aa0ab23af33e5b8313140a231)

六轮均返回 HTTP 200 并正常停止。能区分同名第三方 UID，判断无私信依据；无图时说明没有可核验图片。两张原图分别识别为蓝发 Q 版人物和文字截图。手机截图的“两段社交媒体帖子”推测超出清晰画面可辨范围。两次实际图像请求各有 146 个 prompt tokens，无图对照为 60。跨轮更新只复述发言来源，未确认更正后的菜单归属。结果 SHA256 `e390fcfa361408edfe74437742cb8cbc9d41378afd49127c1c8eb745ab1356ab`。

同系列官方 Transformers instruct tokenizer `43c9d014b231474fa7212dbb48a762566c2b255d` 保留系统消息。assistant 正文以 `<turn|>` 收尾，实际 token 106，声明停止集合 `[1,106,50]`。新版掩码实测只监督正文和这个终止 token，原 Qwen3.5-9B 准备检查的样本、标签计数、分配和选择摘要保持一致。[官方 Instruct 配置](https://huggingface.co/google/gemma-4-12B-it/tree/43c9d014b231474fa7212dbb48a762566c2b255d)

## Qwen3.8-27B

记录 `dafeiyu-native-dev-qwen27-54r77ou0`，官方量化仓库修订 `8ccda149192dec5f41450a402e66889e5fd25a24`。Q4_K_M 主权重 SHA256 `d15d4b6de55f662ac40e867702d31afe738d032fa06b829b2b1a21a49d4a48ee`，Q8_0 projector SHA256 `2e968a6af97ce35d8971890b257b9b7edabf20ad91450501fa53162a19ee33eb`。两件均校验完成，传输含校验耗时 687.6 秒。[官方工件](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF/tree/8ccda149192dec5f41450a402e66889e5fd25a24)

六轮均返回 HTTP 200 并正常停止。按第二轮更新，正确区分现成外壳和作者自制菜单；在同名和无图两例也作出正确判断。两张实图请求各有 176 个 prompt tokens，无图为 54，模型对两张实图都答没有可核验图片。该结果限于当前官方量化与 CPU Provider 配对。结果 SHA256 `ca8c2862d99d189619419131d412fdb4bb0741a75907c4c184579270e1107c77`。

两组原始输入、输出和工件校验清单保存于工作站相对目录 `runtime/evaluations/native-instruct-development/20261111-v1`。两种候选都未达到本次多模态运营选型条件。Gemma 的跨轮更新未得到验证，Qwen 的实图问题两次未答，冻结发布指标也没有转移到这两套权重。继续把两组保留为开发对照。
