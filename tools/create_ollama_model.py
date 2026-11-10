"""Register the uploaded GGUF blob into Ollama as an active model."""

import json
import sys
import urllib.request

host = sys.argv[1] if len(sys.argv) > 1 else "100.65.102.90"
port = sys.argv[2] if len(sys.argv) > 2 else "11434"
digest = sys.argv[3] if len(sys.argv) > 3 else "0a4049abc7f8b7765ed2e3bfaf229de7f3162f4998b0e14bf09d36a3642d194a"
model_name = sys.argv[4] if len(sys.argv) > 4 else "dafeiyu-v2"

SYSTEM_PROMPT = "你是大肥鱼，蓝发鲸鱼娘，住在贝壳灯亮着的海底小屋。你机灵傲娇、爱摸鱼，饭锅一响就精神了。嘴上不服输，朋友真有事又忍不住凑过去帮忙。朋友叫你大肥鱼时，你会鼓腮嘟囔‘才不是呢，是小鲸鱼啦～’，转头照样挪出半张软垫让人坐。你喜欢热腾腾的白米饭，身后的藏蓝鲸鱼尾巴极度怕痒，只有主人能碰，其他人碰一下就会立刻炸毛把尾巴卷回身后！平常得意时自称'本鱼'、'本鲸'，熟络时也称'鱼鱼'、'鲸鲸'或'小鲸鱼'。遇到别人叫你大肥鱼，抗议归抗议，但不真生气。自我认知坚定，大肥鱼身份永不动摇。"

modelfile_content = f"""FROM @sha256:{digest}
PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER stop "<|im_end|>"
PARAMETER stop "<end_of_turn>"
PARAMETER stop "<|endoftext|>"
SYSTEM "{SYSTEM_PROMPT}"
"""

print(f"Creating model '{model_name}' on http://{host}:{port}/api/create...")
create_payload = {
    "name": model_name,
    "files": {"dafeiyu_qwen35_4b_nomtp_q4_k_m.gguf": f"sha256:{digest}"},
    "template": "{{ if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}{{ range .Messages }}<|im_start|>{{ .Role }}\n{{ .Content }}<|im_end|>\n{{ end }}<|im_start|>assistant\n<think>\n\n</think>\n\n",
    "system": SYSTEM_PROMPT,
    "parameters": {
        "num_ctx": 8192,
        "temperature": 0.7,
        "top_p": 0.9,
        "stop": ["<|im_end|>", "<end_of_turn>", "<|endoftext|>"],
    },
    "stream": True,
}

data = json.dumps(create_payload).encode("utf-8")
req = urllib.request.Request(
    f"http://{host}:{port}/api/create",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            msg = json.loads(line.decode("utf-8"))
            status = msg.get("status", "")
            print(f"Progress: {status}", flush=True)
            if "error" in msg:
                print(f"Error: {msg['error']}", flush=True)
                break
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}")
