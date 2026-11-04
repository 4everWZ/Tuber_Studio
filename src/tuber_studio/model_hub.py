"""Model Hub for automatic discovery, validation, and on-demand download of fine-tuned models."""

import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "MiniCPM-V-4.7-1B (大肥鱼专属微调)"
DEFAULT_MODEL_FILENAME = "MiniCPM-V-4_7-1B-dafeiyu-Q4_K_M.gguf"
DEFAULT_REPO_ID = "4everImmortality/dafeiyu-minicpmv"
MIRROR_HF_URL = f"https://hf-mirror.com/{DEFAULT_REPO_ID}/resolve/main/{DEFAULT_MODEL_FILENAME}"
OFFICIAL_HF_URL = f"https://huggingface.co/{DEFAULT_REPO_ID}/resolve/main/{DEFAULT_MODEL_FILENAME}"
EXPECTED_MODEL_SIZE = 672003776  # ~640.9 MB

_lock = threading.Lock()
_active_thread: threading.Thread | None = None
_cancel_flag = False

_download_state = {
    "status": "idle",  # "idle" | "downloading" | "completed" | "failed" | "cancelled"
    "downloaded_bytes": 0,
    "total_bytes": EXPECTED_MODEL_SIZE,
    "percent": 0.0,
    "speed_mb_s": 0.0,
    "error": None,
    "target_path": None,
}


def _default_models_dir() -> Path:
    from .config import ROOT

    runtime_dir = Path(os.getenv("TUBER_RUNTIME", ROOT / "runtime"))
    models_dir = runtime_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


def is_model_installed(models_dir: Path | None = None) -> bool:
    """Check if the default fine-tuned model exists and has a valid file size."""
    target_dir = models_dir or _default_models_dir()
    model_path = target_dir / DEFAULT_MODEL_FILENAME
    if not model_path.is_file():
        return False
    size = model_path.stat().st_size
    # Must be at least 600 MB (avoid corrupted stub/partial files)
    return size >= 600 * 1024 * 1024


def get_model_status(models_dir: Path | None = None) -> dict:
    """Return the current installation and download status of the fine-tuned model."""
    target_dir = models_dir or _default_models_dir()
    model_path = target_dir / DEFAULT_MODEL_FILENAME
    installed = is_model_installed(target_dir)

    with _lock:
        state = dict(_download_state)

    current_size = model_path.stat().st_size if model_path.is_file() else 0

    return {
        "model_name": DEFAULT_MODEL_NAME,
        "filename": DEFAULT_MODEL_FILENAME,
        "repo_id": DEFAULT_REPO_ID,
        "installed": installed,
        "file_path": str(model_path.resolve()) if installed else None,
        "file_size": current_size,
        "expected_size": EXPECTED_MODEL_SIZE,
        "download_status": "completed" if installed and state["status"] == "idle" else state["status"],
        "download_percent": 100.0 if installed and state["status"] == "idle" else state["percent"],
        "downloaded_bytes": current_size if installed and state["status"] == "idle" else state["downloaded_bytes"],
        "speed_mb_s": state["speed_mb_s"],
        "error": state["error"],
    }


def _download_worker(target_dir: Path):
    global _cancel_flag
    target_file = target_dir / DEFAULT_MODEL_FILENAME
    temp_file = target_dir / f"{DEFAULT_MODEL_FILENAME}.download.tmp"

    urls_to_try = [MIRROR_HF_URL, OFFICIAL_HF_URL]

    with _lock:
        _download_state["status"] = "downloading"
        _download_state["downloaded_bytes"] = 0
        _download_state["total_bytes"] = EXPECTED_MODEL_SIZE
        _download_state["percent"] = 0.0
        _download_state["speed_mb_s"] = 0.0
        _download_state["error"] = None
        _download_state["target_path"] = str(target_file)

    logger.info("Starting fine-tuned model download to %s", target_file)

    for url in urls_to_try:
        if _cancel_flag:
            break
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TuberStudio/1.0 (DaFeiyu-ModelHub)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                total_len = int(resp.headers.get("Content-Length") or EXPECTED_MODEL_SIZE)
                with _lock:
                    _download_state["total_bytes"] = total_len

                downloaded = 0
                chunk_size = 256 * 1024
                start_time = time.time()
                last_speed_time = start_time
                bytes_since_last_speed = 0

                with open(temp_file, "wb") as f:
                    while True:
                        if _cancel_flag:
                            logger.info("Model download was cancelled by user")
                            with _lock:
                                _download_state["status"] = "cancelled"
                                _download_state["error"] = "用户取消下载"
                            if temp_file.exists():
                                temp_file.unlink(missing_ok=True)
                            return

                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        bytes_since_last_speed += len(chunk)

                        now = time.time()
                        if now - last_speed_time >= 0.5:
                            duration = now - last_speed_time
                            speed = (bytes_since_last_speed / (1024 * 1024)) / duration
                            last_speed_time = now
                            bytes_since_last_speed = 0
                            with _lock:
                                _download_state["downloaded_bytes"] = downloaded
                                _download_state["percent"] = round((downloaded / total_len) * 100, 1)
                                _download_state["speed_mb_s"] = round(speed, 2)

                # Successful download, atomic replace
                if temp_file.stat().st_size >= total_len * 0.95:
                    if target_file.exists():
                        target_file.unlink()
                    temp_file.replace(target_file)
                    with _lock:
                        _download_state["status"] = "completed"
                        _download_state["downloaded_bytes"] = total_len
                        _download_state["percent"] = 100.0
                        _download_state["speed_mb_s"] = 0.0
                        _download_state["error"] = None
                    logger.info("Fine-tuned model successfully downloaded and installed to %s", target_file)
                    return
        except Exception as e:
            logger.warning("Download from %s failed: %s; trying next URL if available", url, e)
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            continue

    with _lock:
        if _download_state["status"] != "cancelled":
            _download_state["status"] = "failed"
            _download_state["error"] = "未能从所有镜像源下载模型，请检查网络或重试"


def start_model_download(models_dir: Path | None = None, force: bool = False) -> dict:
    """Start background downloading the fine-tuned model if not installed or forced."""
    global _active_thread, _cancel_flag
    target_dir = models_dir or _default_models_dir()

    if is_model_installed(target_dir) and not force:
        return {"status": "already_installed", "message": "模型已存在于本地目录"}

    with _lock:
        if _download_state["status"] == "downloading" and _active_thread and _active_thread.is_alive():
            return {"status": "in_progress", "message": "模型下载任务已在进行中"}
        _cancel_flag = False
        _download_state["status"] = "downloading"
        _download_state["error"] = None

    _active_thread = threading.Thread(
        target=_download_worker,
        args=(target_dir,),
        name="DaFeiyuModelDownloader",
        daemon=True,
    )
    _active_thread.start()
    return {"status": "started", "message": "已在后台启动模型下载任务"}


def cancel_model_download():
    """Cancel any active downloading task."""
    global _cancel_flag
    _cancel_flag = True
    with _lock:
        if _download_state["status"] == "downloading":
            _download_state["status"] = "cancelled"
            _download_state["error"] = "已请求取消下载"


def ensure_model_on_first_run(models_dir: Path | None = None) -> bool:
    """Check if model exists on first run; if not, trigger background download."""
    target_dir = models_dir or _default_models_dir()
    if is_model_installed(target_dir):
        return True
    logger.info("First run check: Da Feiyu fine-tuned model not found in %s. Initiating auto-download...", target_dir)
    start_model_download(target_dir, force=False)
    return False
