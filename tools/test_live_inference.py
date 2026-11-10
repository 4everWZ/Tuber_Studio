import json
import time
import urllib.request

host = "100.65.102.90"
port = "11434"
model = "dafeiyu-v2"

prompt = "大肥鱼！叫你呢大肥鱼！今晚吃啥？"
print(f"Testing live inference on http://{host}:{port} with model '{model}'...")
print(f"User prompt: {prompt}")

payload = {
    "model": model,
    "messages": [
        {"role": "user", "content": prompt}
    ],
    "stream": True,
    "options": {
        "num_ctx": 8192,
        "temperature": 0.7,
        "top_p": 0.9,
    },
}

t0 = time.time()
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    f"http://{host}:{port}/api/chat",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    print("\n--- Response Stream ---")
    in_thinking = False
    eval_count = 0
    eval_duration = 0
    prompt_eval_count = 0
    prompt_eval_duration = 0
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            chunk = json.loads(line.decode("utf-8"))
            msg = chunk.get("message", {})
            thinking = msg.get("thinking", "")
            content = msg.get("content", "")
            
            if thinking:
                if not in_thinking:
                    print("\n[Thought Process]: ", end="", flush=True)
                    in_thinking = True
                print(thinking, end="", flush=True)
                
            if content:
                if in_thinking:
                    print("\n\n[Dafeiyu]: ", end="", flush=True)
                    in_thinking = False
                print(content, end="", flush=True)
                
            if chunk.get("done", False):
                eval_count = chunk.get("eval_count", 0)
                eval_duration = chunk.get("eval_duration", 1) / 1e9
                prompt_eval_count = chunk.get("prompt_eval_count", 0)
                prompt_eval_duration = chunk.get("prompt_eval_duration", 1) / 1e9
        t1 = time.time()
        
    total_time = t1 - t0
    speed = eval_count / eval_duration if eval_duration > 0 else 0
    prompt_speed = prompt_eval_count / prompt_eval_duration if prompt_eval_duration > 0 else 0

    print(f"\n\n--- Performance Metrics ---")
    print(f"Total time: {total_time:.2f}s")
    print(f"Prompt processing: {prompt_eval_count} tokens in {prompt_eval_duration:.2f}s ({prompt_speed:.2f} tok/s)")
    print(f"Generation speed: {eval_count} tokens in {eval_duration:.2f}s ({speed:.2f} tok/s)")
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}")
