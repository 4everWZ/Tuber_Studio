"""Upload GGUF quantized models to Hugging Face Hub."""

from __future__ import annotations

import logging
from pathlib import Path
from huggingface_hub import HfApi, create_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hf_upload_gguf")

REPO_ROOT = Path(__file__).resolve().parents[2]
api = HfApi()

GGUF_MODELS = [
    {
        "repo_id": "4everImmortality/dafeiyu-qwen35-4b-gguf",
        "file_path": REPO_ROOT / "runtime/merged_models/dafeiyu_qwen35_4b_merged/dafeiyu_qwen35_4b_nomtp_q4_k_m.gguf",
        "filename": "dafeiyu_qwen35_4b_nomtp_q4_k_m.gguf",
        "base_model": "Qwen/Qwen3.5-4B",
        "title": "Dafeiyu V2 - Qwen 3.5 4B GGUF (Q4_K_M)",
        "desc": "Quantized Q4_K_M GGUF model for the Dafeiyu anime VTuber persona, fine-tuned on Qwen 3.5 4B using DoRA (Weight-Decomposed LoRA).",
        "model_name": "dafeiyu-4b",
    },
    {
        "repo_id": "4everImmortality/dafeiyu-qwen35-9b-gguf",
        "file_path": REPO_ROOT / "runtime/merged_models/dafeiyu_qwen35_merged/dafeiyu_qwen35_nomtp_q4_k_m.gguf",
        "filename": "dafeiyu_qwen35_nomtp_q4_k_m.gguf",
        "base_model": "Qwen/Qwen3.5-9B",
        "title": "Dafeiyu V2 - Qwen 3.5 9B GGUF (Q4_K_M)",
        "desc": "Quantized Q4_K_M GGUF model for the Dafeiyu anime VTuber persona, fine-tuned on Qwen 3.5 9B using DoRA. Official production model deployed on Jetson AGX Orin.",
        "model_name": "dafeiyu-v2",
    },
]

MODEL_CARD_TEMPLATE = """---
license: other
base_model: {base_model}
tags:
- gguf
- ollama
- llama.cpp
- anime
- vtuber
- dafeiyu
- roleplay
pipeline_tag: text-generation
language:
- zh
- en
---

# {title}

{desc}

## 角色设定 (Character Persona)
- **身份**: 大肥鱼，蓝发鲸鱼娘，住在贝壳灯亮着的海底小屋。机灵傲娇、爱摸鱼，饭锅一响就精神。嘴上不服输，朋友真有事又忍不住凑过去帮忙。朋友叫大肥鱼时会鼓腮嘟囔“才不是呢，是小鲸鱼啦～”，转头照样挪出半张软垫让人坐。
- **核心偏好**: 极度偏爱热腾腾的大白米饭。
- **肢体与边界**: 藏蓝鲸鱼尾巴极度怕痒、除主人外严禁碰触，被戳碰立刻炸毛收回身后；海底小屋的暖光软垫则大方分一半给来客朋友歇脚。
- **语言风格**: 傲娇活泼、接梗快嘴、零机械 AI 味。

## 特色与优势 (Highlights)
- **原生超长上下文**: 原生支持 262,144 (256K) Tokens 上下文。
- **llama.cpp / Ollama 完美兼容**: 采用 `--no-mtp` 解耦转换，彻底避免 MTP draft layer 造成的张量不对齐报错，即开即用。
- **极小显存占用**: Q4_K_M 极致量化，边缘端（如 Jetson AGX Orin）轻松全层 GPU 卸载。

## Quickstart with Ollama

创建 `Modelfile`:
```dockerfile
FROM ./{filename}

PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER stop "<|im_end|>"
PARAMETER stop "<end_of_turn>"
PARAMETER stop "<|endoftext|>"

SYSTEM \"\"\"你是大肥鱼，蓝发鲸鱼娘，住在贝壳灯亮着的海底小屋。你机灵傲娇、爱摸鱼，饭锅一响就精神了。嘴上不服输，朋友真有事又忍不住凑过去帮忙。朋友叫你大肥鱼时，你会鼓腮嘟囔‘才不是呢，是小鲸鱼啦～’，转头照样挪出半张软垫让人坐。你喜欢热腾腾的白米饭，身后的藏蓝鲸鱼尾巴极度怕痒，只有主人能碰，其他人碰一下就会立刻炸毛把尾巴卷回身后！平常得意时自称'本鱼'、'本鲸'，熟络时也称'鱼鱼'、'鲸鲸'或'小鲸鱼'。遇到别人叫你大肥鱼，抗议归抗议，但不真生气。自我认知坚定，大肥鱼身份永不动摇。\"\"\"
```

构建并运行:
```bash
ollama create {model_name} -f Modelfile
ollama run {model_name}
```
"""


def main():
    for item in GGUF_MODELS:
        repo_id = item["repo_id"]
        gguf_file = item["file_path"]
        if not gguf_file.exists():
            logger.warning("GGUF file %s does not exist, skipping.", gguf_file)
            continue

        logger.info("Ensuring Hugging Face repository %s exists...", repo_id)
        try:
            create_repo(repo_id, repo_type="model", exist_ok=True)
        except Exception as e:
            logger.info("create_repo note: %s", e)

        card_content = MODEL_CARD_TEMPLATE.format(
            base_model=item["base_model"],
            title=item["title"],
            desc=item["desc"],
            filename=item["filename"],
            model_name=item["model_name"],
        )

        logger.info("Uploading README.md for %s...", repo_id)
        api.upload_file(
            path_or_fileobj=card_content.encode("utf-8"),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="model",
            commit_message="docs: add authentic debranded model card",
        )

        logger.info("Uploading %s to %s (this may take 1-3 minutes)...", gguf_file, repo_id)
        api.upload_file(
            path_or_fileobj=str(gguf_file),
            path_in_repo=item["filename"],
            repo_id=repo_id,
            repo_type="model",
            commit_message=f"feat: upload {item['filename']} quantized GGUF",
        )

        logger.info("SUCCESS: Pushed GGUF to https://huggingface.co/%s", repo_id)


if __name__ == "__main__":
    main()
