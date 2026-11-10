"""Stream a large GGUF file to Ollama API as a sha256 blob."""

import sys
import time
import requests

digest = sys.argv[1]
filepath = sys.argv[2]
url = f"http://100.65.102.90:11434/api/blobs/sha256:{digest}"

print(f"Uploading {filepath} to {url}...")
total_bytes = 0
t0 = time.time()

def gen_chunks(path, chunk_size=8 * 1024 * 1024):
    global total_bytes
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            mb = total_bytes / (1024 * 1024)
            elapsed = time.time() - t0
            speed = mb / elapsed if elapsed > 0 else 0
            print(f"\rUploaded: {mb:.1f} MB ({speed:.1f} MB/s)...", end="", flush=True)
            yield chunk

resp = requests.post(url, data=gen_chunks(filepath), headers={"Content-Type": "application/octet-stream"})
print(f"\nFinished: HTTP {resp.status_code} - {resp.text}")
