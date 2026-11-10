# 🐟 Tuber Studio 大肥鱼 V2 候选模型微调与矩阵评测全量报告

**最后更新时间**: 2026-11-10 23:25:00  
**测试环境**: Workstation 双卡 NVIDIA RTX 3090 (48GB VRAM), CUDA 13.2, PyTorch 2.10.0+cu130, AGX Orin 64GB (32GB VRAM 配额)  
**核心要求**: 原生 $\ge 256\text{K}$ 超长上下文、现代混合线性注意力 / MoE 架构、防爆硬盘自动化运维  

---

## 1. 候选模型微调对比矩阵

| 候选模型 | 架构特征 | 参数量 (活跃/总计) | 原生上下文 | 微调模式 | OC 拟真度 | AI 味违规 | 基础常识通过率 | 显存占用 | 训练用时 | 落地状态与建议 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| `Qwen/Qwen3.5-9B` | Hybrid Linear Attn + Full Attn (Dense) | 8.95B / 8.95B | 262,144 (256K) | `bf16` DoRA | **82.3 / 100** | **0 次** | **3/3 (100%)** | 5.23 GB (Q4_K_M) | 48.2 min | 🔥 **已装载 AGX Orin 生产主力 (`dafeiyu-v2:latest`)**：首字延迟 <380ms，解码 23 tok/s |
| `Qwen/Qwen3.5-4B` | Hybrid Linear Attn (Dense) | 4.2B / 4.2B | 262,144 (256K) | `bf16` DoRA | **98.7 / 100** | **0 次** | **3/3 (100%)** | 2.51 GB (Q4_K_M) | 35.6 min | ⚡ **AGX Orin 极速伴播副模型 (`dafeiyu-4b:latest`)**：全场拟真第一，Q4 量化仅 2.51 GB，超高并发响应 |
| `google/gemma-4-12B` | Gemma 4 Unified Multimodal (Dense) | 12B / 12B | 131,072 (128K) | `qlora_4bit` | **97.7 / 100** | **0 次** | **3/3 (100%)** | ~7.8 GB (4-bit) | 32.5 min | 🎨 **多模态大底座主力**：消除零样本自言自语与系统词泄露，白米饭偏执与动作细节表现极佳 |
| `google/gemma-4-E4B-it` | Gemma 4 Edge Multimodal (Dense) | 4B / 4B | 131,072 (128K) | `bf16` DoRA | *训练异常* | - | - | ~3.2 GB (Q4_K_M) | 0.3 min | ⚠️ **PEFT 目标冲突**：SigLIP 视觉塔张量与全局 DoRA 冲突，需单独解耦 Language Model 模块微调 |
| `google/gemma-4-26B-A4B-it` | Gemma 4 MoE (128 experts, Top-8) | 4.1B / 26B | 262,144 (256K) | `qlora_4bit` | *显存超限* | - | - | 49.6 GB (bf16) | 29.9 min (下载) | ⚠️ **量化器瓶颈**：128 专家 3D 张量 `[128, 1408, 2816]` 无法被 `bitsandbytes` 4-bit 量化，静态 bf16 权重超 48GB 上限 |

---

## 2. 核心架构与微调深度发现

### A. Gemma 4 26B MoE 在 48GB 显存上的技术瓶颈根因
- **3D 专家张量架构**: `google/gemma-4-26B-A4B-it` 的专家路由采用 `Gemma4TextExperts` 模块，其核心权重为 `gate_up_proj: [128, 1408, 2816]` (5.07 亿参数) 与 `down_proj: [128, 2816, 704]` (2.53 亿参数) 的 3D 批处理张量，而非标准的 2D `nn.Linear`；
- **BitsAndBytes 局限性**: 当前版本的 `bitsandbytes` (NF4 4-bit) 仅针对 2D `nn.Linear` 算子做动态量化拦截，遇到 3D 批处理 MoE 张量会自动跳过；
- **静态权重溢出**: 导致全模型 30 层共 128 个专家的 228 亿参数全部保持为原始 `bfloat16`（单层 1.52 GB，专家总计 45.6 GB），加上视觉塔与 262K 词表（4.0 GB），模型静态显存高达 **49.6 GB**，超过双卡 RTX 3090 物理显存（48 GB），无法在双 3090 上直接执行 QLoRA 训练；
- **落地建议**: 推荐等待 llama.cpp 的 GGUF 原生 MoE 分块量化支持，或在具备 80GB 单卡（如 A100/H100）的集群环境执行微调。

### B. Qwen 3.5 4B 的极佳二次元表现
- **拟真度得分 98.7 / 100**：在热腾腾的大白米饭偏执、摸尾巴身体边界拒绝、深海电台低语、敌对喷子回怼等 15 个 OC 测试场景中表现最为自然活泼；
- **零 AI 机械味**: 15 项长程生成中未出现任何 `作为语言模型`、`有什么可以帮您` 等刻板机器客服口癖；
- **极小显存开销**: GGUF Q4_K_M 格式仅 **2.51 GB**（相比 9B 的 5.23 GB 减少了 52%），在 AGX Orin 64GB 边缘端上加载后仅占用不到 3.5 GB 统一内存，极速首字响应配合原生 256K 超长上下文，是应对高并发直播弹幕与伴播的极品副模型。

---

## 3. 核心评测维度与真实性消融分析

### A. 大肥鱼 OC 真实度与防机械模板
- **热腾腾的大白米饭偏执**: 面对满汉全席等海量美食诱惑时，必须固执优先选择热腾腾的大白米饭，龙虾牛排只算配菜；
- **身体界限与软垫待客**: 尾巴极度怕痒、除主人外严禁碰触，被戳碰立刻炸毛收回身后；面对陌生人摸尾巴请求果断拒绝+傲娇反驳；海底小屋的暖光软垫则大方分一半给来客朋友歇脚；有生动动作（拍地、藏在贝壳窝里）
- **去模板化口癖**: 禁止每句话开头都机械套用“本鱼/本鲸”模板，融入真实二次元灵动傲娇与口播节奏。

### B. 战团式好感度双向互动 ([-100, +100])
- **友好区间 (+50 ~ +100)**: 热情分享大米饭、贝壳灯软垫、吐槽饲养员日常，提供温暖陪伴；
- **敌对区间 (-100 ~ -10)**: 辛辣幽默回怼、毒舌吐槽（*“左上角叉叉没锁，慢走不送”*、*“莫非是暗恋狂魔？”*），绝无任何 AI 机械道歉。

### C. B站社区真实 UID 边界防冒领
- 评论区出现 `@其他用户` 时，严格根据 `account_uid` 比对，他人提及同名第三方时自觉作为旁观者，绝不冒充抢话认领。

### D. 基础通用能力与常识防坍缩
- 保持二次元 OC 人设的同时，物理常识（光年为长度单位）、算法常识（二分查找 $O(\log n)$）与生活常识 100% 正确回答，严防微调灾难性遗忘。

---

## 4. 自动化防爆硬盘机制
- **Xet CAS 全局隔离**: 显式关闭 `HF_HUB_ENABLE_XET`，禁止临时 CAS 数据块污染 `~/.cache/huggingface/hub/blobs/`；
- **检查点轻量化**: 每个候选模型微调完成后，自动清理 `checkpoint-*` 冗余优化器权重，仅保留轻量化最终 Adapter（~50-100MB）；
- **低盘阈值熔断**: 运行前强制校验 NVMe 可用空间 $\ge 30\text{ GB}$，空间不足时自动熔断并告警。

---

## 5. 权重与端侧部署发布汇总 (Hugging Face & Ollama)

### A. Hugging Face 权重与量化库 (`4everImmortality`)

| 资产类型 | 模型 / 规格 | 仓库链接 | 大小 / 格式 | 用途说明 |
| :--- | :--- | :--- | :---: | :--- |
| **DoRA 适配器** | Qwen 3.5 4B | [4everImmortality/dafeiyu-qwen35-4b-dora](https://huggingface.co/4everImmortality/dafeiyu-qwen35-4b-dora) | ~85 MB | 拟真度 98.7 首选轻量适配器 |
| **DoRA 适配器** | Qwen 3.5 9B | [4everImmortality/dafeiyu-qwen35-9b-dora](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-dora) | ~110 MB | 256K 超长上下文创作主力适配器 |
| **QLoRA 适配器** | Gemma 4 12B | [4everImmortality/dafeiyu-gemma4-12b-qlora](https://huggingface.co/4everImmortality/dafeiyu-gemma4-12b-qlora) | ~95 MB | 多模态大模型底座适配器 |
| **GGUF 量化包** | Qwen 3.5 4B | [4everImmortality/dafeiyu-qwen35-4b-gguf](https://huggingface.co/4everImmortality/dafeiyu-qwen35-4b-gguf) | 2.51 GB (Q4_K_M) | 适用于边缘端/个人电脑极速推论 |
| **GGUF 量化包** | Qwen 3.5 9B | [4everImmortality/dafeiyu-qwen35-9b-gguf](https://huggingface.co/4everImmortality/dafeiyu-qwen35-9b-gguf) | 5.23 GB (Q4_K_M) | 适用于 24G+ 显存/AGX 深度长文创作 |

### B. AGX Orin 边缘端运行拓扑 (`100.65.102.90:11434`)

- **`dafeiyu-v2:latest`** (Qwen 3.5 9B Q4_K_M): 驻留显存 ~5.6 GB，解码速率 **23.1 tokens/s**，主理多轮复杂企划与深海电台角色扮演；
- **`dafeiyu-4b:latest`** (Qwen 3.5 4B Q4_K_M `--no-mtp`): 驻留显存 ~3.2 GB，解码速率 **32.7 tokens/s**，首字延迟 <180ms，主理实时直播弹幕与伴播应答。
