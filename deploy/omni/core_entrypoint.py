"""Start the normal Tuber Studio workbench with a deployment-scoped Omni key."""

import argparse
import os
from pathlib import Path

import uvicorn

from tuber_studio.app import create_app
from tuber_studio.config import Settings, validate_api_key
from tuber_studio.runtime_authority import acquire_business_writer, read_business_authority

GATEWAY_BASE = "http://omni-gateway:8000/v1"
DEFAULT_MODEL_CHECKPOINT = "fb09628e98b1f55d8d5b863e0eccfcdd11bff499"


def configure_model(data, *, omni):
    window = int(os.environ.get("OMNI_CONTEXT_TOKENS", "12288"))
    data["inference_broker"]["enabled"] = False
    data["llm"].update(
        kind="openai_compatible" if omni else "demo", base_url=GATEWAY_BASE,
        model=os.environ.get("OMNI_MODEL_ALIAS", "omni-default"), context_tokens=window,
        native_context_tokens=window, auto_compact_token_limit=window * 9 // 10,
    )
    if omni:
        data["llm"]["structured_output"] = "json_schema"
        data["video_vision"] = {
            "enabled": True, "adapter": "omni", "base_url": GATEWAY_BASE, "model": data["llm"]["model"],
            "model_revision": os.environ.get("OMNI_VISION_CHECKPOINT") or DEFAULT_MODEL_CHECKPOINT,
            "context_tokens": window, "native_context_tokens": window,
        }


class ComposeSettings(Settings):
    def __init__(self):
        super().__init__()
        mode = os.environ.get("TUBER_CORE_MODE", "omni")
        if mode not in {"omni", "demo", "existing"}:
            raise ValueError("TUBER_CORE_MODE must be omni, demo or existing")
        if not self.path.exists():
            if mode == "existing":
                raise ValueError("An existing Core volume needs its studio.json")
            configure_model(self.data, omni=mode == "omni")
            self.data["tts"]["kind"] = "prerecorded"
            self.save_public(self.data)
        key_file = os.environ.get("OMNI_GATEWAY_TOKEN_FILE")
        self.gateway_key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else ""
        llm = self.data["llm"]
        uses_gateway = llm["kind"] == "openai_compatible" and llm["base_url"].rstrip("/") == GATEWAY_BASE
        if uses_gateway:
            llm.setdefault("structured_output", "json_schema")
        if self.gateway_key or uses_gateway or self._uses_gateway_vision():
            validate_api_key(self.gateway_key)

    def provider_key(self, name):
        # Diagnostics and hot config reload use this method too. Do not let a
        # saved address change send the service-managed key to another endpoint.
        if (
            name == "llm"
            and self.data["llm"]["kind"] == "openai_compatible"
            and self.data["llm"]["base_url"].rstrip("/") == GATEWAY_BASE
            and self.gateway_key
        ):
            return self.gateway_key
        return super().provider_key(name)

    def _uses_gateway_vision(self):
        vision = self.data.get("video_vision", {})
        return (
            vision.get("enabled")
            and vision.get("adapter") == "omni"
            and vision.get("base_url", "").rstrip("/") == GATEWAY_BASE
        )

    def video_vision_key(self):
        if self._uses_gateway_vision():
            return self.gateway_key
        return super().video_vision_key()


def stage_omni():
    runtime = Path(os.environ["TUBER_RUNTIME"]).resolve(strict=True)
    config = runtime / "studio.json"
    with acquire_business_writer(runtime, check_authority=False):
        authority = read_business_authority(runtime)
        if not config.is_file() or config.is_symlink() or not authority or authority["state"] != "staged":
            raise RuntimeError("An existing staged business snapshot is required")
        if Path(os.getenv("TUBER_CONFIG", config)).resolve() != config:
            raise RuntimeError("The staged model profile belongs to the business volume")
        settings = Settings(runtime=runtime)
        configure_model(settings.data, omni=True)
        settings.save_public(settings.data)


def build_app():
    app = create_app(ComposeSettings())

    @app.get("/healthz", include_in_schema=False)
    async def health():
        app.state.store.one("SELECT 1")
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-omni", action="store_true", help="Set Omni on a staged business restore")
    if parser.parse_args().stage_omni:
        stage_omni()
    else:
        uvicorn.run(build_app, factory=True, host="0.0.0.0", port=9020, ws="wsproto")
