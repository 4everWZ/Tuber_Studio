"""Test inference on AGX Orin Ollama endpoint."""
import json
import time
import urllib.request
import sys

host = sys.argv[1] if len(sys.argv) > 1 else "100.65.102.90"
port = sys.argv[2] if len(sys.argv) > 2 else "11434"
model = sys.argv[3] if len(sys.argv) > 3 else "dafeiyu-4b:latest"
prompt = sys.argv[4] if len(sys.argv) > 4 else "大肥鱼！今晚给你煮了一大锅热气腾腾的大白米饭，快来吃！"

payload = {
    "model": model,
    "prompt": prompt,
    "stream": False,
}

print(f"Sending prompt to http://{host}:{port}/api/generate...")
print(f"Model: {model}")
print(f"Prompt: {prompt}")

t0 = time.time()
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    f"http://{host}:{port}/api/generate",
    data=data,
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(req, timeout=120) as resp:
    res = json.loads(resp.read().decode("utf-8"))

total_time = time.time() - t0
eval_count = res.get("eval_count", 0)
eval_duration_s = res.get("eval_duration", 1) / 1e9
speed = eval_count / eval_duration_s if eval_duration_s > 0 else 0

print("=" * 60)
print("Response:")
print(res.get("response", ""))
print("=" * 60)
print(f"Total time: {total_time:.2f} s")
print(f"Prompt tokens: {res.get('prompt_eval_count')}")
print(f"Generated tokens: {eval_count}")
print(f"Generation speed: {speed:.1f} tok/s")
