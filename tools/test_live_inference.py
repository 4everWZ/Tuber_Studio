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
    "stream": False,
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        t1 = time.time()
        res = json.loads(resp.read().decode("utf-8"))
        
    total_time = t1 - t0
    message = res.get("message", {}).get("content", "")
    eval_count = res.get("eval_count", 0)
    eval_duration = res.get("eval_duration", 1) / 1e9
    speed = eval_count / eval_duration if eval_duration > 0 else 0

    print(f"\nDafeiyu Response: {message}")
    print(f"Total time: {total_time:.2f}s | Eval count: {eval_count} tokens | Speed: {speed:.2f} tokens/s")
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}")
