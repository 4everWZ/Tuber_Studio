"""Upload fine-tuned Dafeiyu models to Hugging Face Hub."""

from __future__ import annotations

import logging
from pathlib import Path
from huggingface_hub import HfApi, create_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hf_upload")

api = HfApi()

MODELS = [
    {
        "repo_id": "4everImmortality/dafeiyu-qwen35-4b-dora",
        "folder": Path("output_queue/adapter_qwen35_4b"),
        "base_model": "Qwen/Qwen3.5-4B",
        "title": "Dafeiyu V2 - Qwen 3.5 4B DoRA Persona Adapter",
        "desc": "Qwen 3.5 4B fine-tuned with DoRA (Weight-Decomposed LoRA) for the Dafeiyu anime VTuber persona. Scores 98.7/100 authenticity, featuring 256K native context and zero AI cliches.",
    },
    {
        "repo_id": "4everImmortality/dafeiyu-qwen35-9b-dora",
        "folder": Path("output_queue/adapter_qwen35_9b"),
        "base_model": "Qwen/Qwen3.5-9B",
        "title": "Dafeiyu V2 - Qwen 3.5 9B DoRA Persona Adapter",
        "desc": "Qwen 3.5 9B fine-tuned with DoRA for the Dafeiyu anime VTuber persona. Active edge production model for deep content creation and long context interaction.",
    },
    {
        "repo_id": "4everImmortality/dafeiyu-gemma4-12b-qlora",
        "folder": Path("output_queue/adapter_gemma4_12b"),
        "base_model": "google/gemma-4-12B",
        "title": "Dafeiyu V2 - Gemma 4 12B QLoRA Persona Adapter",
        "desc": "Gemma 4 12B multimodal foundation fine-tuned with 4-bit QLoRA for the Dafeiyu anime VTuber persona. Scores 97.7/100 authenticity.",
    },
]

MODEL_CARD_TEMPLATE = """---
library_name: peft
base_model: {base_model}
tags:
- peft
- dora
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

## 使用方式 (Usage with PEFT & Transformers)

```python
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

base_model = "{base_model}"
adapter_id = "{repo_id}"

tokenizer = AutoTokenizer.from_pretrained(adapter_id, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    base_model,
    torch_dtype=torch.bfloat16,
    device_map="auto",
    trust_remote_code=True,
)
model = PeftModel.from_pretrained(model, adapter_id)
model.eval()

messages = [
    {{"role": "system", "content": "你是大肥鱼，蓝发鲸鱼娘，住在贝壳灯亮着的海底小屋。你机灵傲娇、爱摸鱼，饭锅一响就精神了。嘴上不服输，朋友真有事又忍不住凑过去帮忙。朋友叫你大肥鱼时，你会鼓腮嘟囔‘才不是呢，是小鲸鱼啦～’，转头照样挪出半张软垫让人坐。你喜欢热腾腾的白米饭，身后的藏蓝鲸鱼尾巴极度怕痒，只有主人能碰，其他人碰一下就会立刻炸毛把尾巴收回去！平常得意时自称'本鱼'、'本鲸'，熟络时也称'鱼鱼'、'鲸鲸'或'小鲸鱼'。遇到别人叫你大肥鱼，抗议归抗议，但不真生气。自我认知坚定，大肥鱼身份永不动摇。"}},
    {{"role": "user", "content": "大肥鱼，今晚给你准备了满汉全席，有龙虾牛排蛋糕，你最想先吃哪个？"}},
]

prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
outputs = model.generate(**inputs, max_new_tokens=160, temperature=0.7, top_p=0.9)
print(tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True))
```
"""


def main():
    repo_root = Path(__file__).resolve().parents[2]
    for item in MODELS:
        repo_id = item["repo_id"]
        folder = repo_root / item["folder"]
        if not folder.exists():
            logger.warning("Folder %s does not exist, skipping.", folder)
            continue

        logger.info("Ensuring HuggingFace repo: %s", repo_id)
        try:
            create_repo(repo_id=repo_id, exist_ok=True, repo_type="model")
        except Exception as e:
            logger.info("create_repo note: %s", e)

        # Write clean README.md
        card_content = MODEL_CARD_TEMPLATE.format(
            base_model=item["base_model"],
            title=item["title"],
            desc=item["desc"],
            repo_id=repo_id,
        )
        readme_path = folder / "README.md"
        readme_path.write_text(card_content, encoding="utf-8")
        logger.info("Wrote clean debranded README.md to %s", readme_path)

        logger.info("Uploading folder %s to %s...", folder, repo_id)
        api.upload_folder(
            folder_path=str(folder),
            repo_id=repo_id,
            repo_type="model",
            commit_message=f"feat: upload {item['title']} with clean debranded persona",
        )
        logger.info("Successfully uploaded %s to Hugging Face!", repo_id)


if __name__ == "__main__":
    main()
