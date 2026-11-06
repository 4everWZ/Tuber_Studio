"""Model Hub for automatic discovery, validation, and on-demand download of fine-tuned models."""

import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

from .inference_profiles import DAFEIYU_ALIASES, DAFEIYU_MODEL, DAFEIYU_WEIGHTS

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = f"{DAFEIYU_MODEL} (大肥鱼专属微调)"
DEFAULT_MODEL_FILENAME = DAFEIYU_WEIGHTS
DEFAULT_REPO_ID = "4everImmortality/dafeiyu-minicpmv"
LEGACY_TEXT_REVISION = "9a0af22b60f794fd885cf4f87bb78f920dd5370b"
_LEGACY_DOWNLOAD = f"{DEFAULT_REPO_ID}/resolve/{LEGACY_TEXT_REVISION}/{DEFAULT_MODEL_FILENAME}"
MIRROR_HF_URL = f"https://hf-mirror.com/{_LEGACY_DOWNLOAD}"
OFFICIAL_HF_URL = f"https://huggingface.co/{_LEGACY_DOWNLOAD}"
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


def get_deployment_category(settings_or_data) -> str:
    """Categorize the deployment into:
    - 'local_file': Running local llama.cpp on this host machine using local model weights file
    - 'self_hosted': Dedicated inference node / Jetson / Docker / external local server (e.g. port 9012 or LAN IP)
    - 'cloud_api': Cloud / remote commercial API (openai_compatible with external domain)
    """
    if hasattr(settings_or_data, "data"):
        data = settings_or_data.data
    elif isinstance(settings_or_data, dict):
        data = settings_or_data
    else:
        return "local_file"

    llm = data.get("llm", {})
    base_url = (
        llm.get("base_url")
        if "base_url" in llm
        else data.get("inference_broker", {}).get("base_url", "") or ""
    ).strip()
    kind = llm.get("kind", "llama_cpp")

    if not base_url:
        return "local_file" if kind == "llama_cpp" else "cloud_api"

    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return "cloud_api" if kind == "openai_compatible" else "local_file"

    is_remote_host = host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"} and bool(host)

    if kind == "openai_compatible":
        is_private_or_local = (
            not is_remote_host
            or host.startswith(
                (
                    "192.168.",
                    "10.",
                    "100.",
                    "172.16.",
                    "172.17.",
                    "172.18.",
                    "172.19.",
                    "172.20.",
                    "172.21.",
                    "172.22.",
                    "172.23.",
                    "172.24.",
                    "172.25.",
                    "172.26.",
                    "172.27.",
                    "172.28.",
                    "172.29.",
                    "172.30.",
                    "172.31.",
                )
            )
            or host.endswith(".local")
        )
        is_known_self_hosted_port = port in {9012, 8000, 11434, 9013, 9010, 9014, 9015, 9030, 19010, 8080}
        if is_known_self_hosted_port or is_private_or_local:
            return "self_hosted"
        return "cloud_api"

    if kind == "llama_cpp":
        if is_remote_host:
            return "self_hosted"
        return "local_file"

    return "local_file"


def is_full_local_deployment(settings_or_data) -> bool:
    """Determine if the deployment is full local on the current host."""
    return get_deployment_category(settings_or_data) == "local_file"


def is_dafeiyu_model(model_name: str | None) -> bool:
    """Check if the given model name refers to the fine-tuned Dafeiyu model."""
    return isinstance(model_name, str) and model_name.strip().lower() in DAFEIYU_ALIASES


def is_model_installed(models_dir: Path | None = None) -> bool:
    """Check if the default fine-tuned model exists and has a valid file size."""
    target_dir = models_dir or _default_models_dir()
    model_path = target_dir / DEFAULT_MODEL_FILENAME
    if not model_path.is_file():
        return False
    size = model_path.stat().st_size
    # Must be at least 600 MB (avoid corrupted stub/partial files)
    return size >= 600 * 1024 * 1024


def get_model_status(models_dir: Path | None = None, settings_or_data=None) -> dict:
    """Return the current installation and download status of the fine-tuned model."""
    target_dir = models_dir or _default_models_dir()
    model_path = target_dir / DEFAULT_MODEL_FILENAME
    installed = is_model_installed(target_dir)

    with _lock:
        state = dict(_download_state)

    current_size = model_path.stat().st_size if model_path.is_file() else 0

    category = get_deployment_category(settings_or_data) if settings_or_data else "local_file"
    is_full_local = category == "local_file"

    llm = {}
    if hasattr(settings_or_data, "data"):
        llm = settings_or_data.data.get("llm", {})
    elif isinstance(settings_or_data, dict):
        llm = settings_or_data.get("llm", {})

    return {
        "model_name": DEFAULT_MODEL_NAME,
        "filename": DEFAULT_MODEL_FILENAME,
        "repo_id": DEFAULT_REPO_ID,
        "installed": installed,
        "is_full_local": is_full_local,
        "deployment_category": category,
        "endpoint_url": (llm.get("base_url") or "").strip(),
        "configured_model": (llm.get("model") or "").strip(),
        "file_path": str(model_path.resolve()) if installed else None,
        "file_size": current_size,
        "expected_size": EXPECTED_MODEL_SIZE,
        "download_status": "completed" if installed and state["status"] == "idle" else state["status"],
        "download_percent": 100.0 if installed and state["status"] == "idle" else state["percent"],
        "downloaded_bytes": current_size
        if installed and state["status"] == "idle"
        else state["downloaded_bytes"],
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
    logger.info(
        "First run check: Da Feiyu fine-tuned model not found in %s. Initiating auto-download...", target_dir
    )
    start_model_download(target_dir, force=False)
    return False
