"""Start the normal Tuber Studio workbench with a deployment-scoped Omni key."""

import argparse
import os
from copy import deepcopy
from pathlib import Path

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from tuber_studio.app import create_app
from tuber_studio.config import Settings, validate_api_key
from tuber_studio.http_api import endpoint
from tuber_studio.runtime_authority import acquire_business_writer, read_business_authority

def gateway_root():
    return endpoint(os.getenv("OMNI_GATEWAY_URL", "http://127.0.0.1:8000"))


def responses_profile(root):
    return {"base_url": root + "/llm/v1", "model": os.getenv("OMNI_LLM_MODEL_ALIAS", "llm-default")}


def configure_omni_llm(llm):
    llm.update(
        context_source="model_card", structured_output="structural_tag", prompt_token_endpoint=True, max_tokens=4096,
        temperature=0.6, top_k=20, top_p=0.95,
    )
    llm.setdefault("enable_thinking", True)


def configure_model(data, *, omni):
    root = gateway_root()
    data["inference_broker"]["enabled"] = False
    data["llm"].update(
        kind="openai_compatible" if omni else "demo", base_url=root + "/v1",
        model=os.environ.get("OMNI_MODEL_ALIAS", "omni-default"),
    )
    if omni:
        data.pop("work_mode_profiles", None)
        configure_omni_llm(data["llm"])
        data["video_vision"] = {
            "enabled": True, "adapter": "omni", "base_url": root + "/v1", "model": data["llm"]["model"],
        }
        if pin := os.getenv("OMNI_VISION_CHECKPOINT"):
            data["video_vision"]["model_revision"] = pin
        data["responses"] = responses_profile(root)


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
        root = gateway_root()
        self.omni_base = root + "/v1"
        self.responses_base = root + "/llm/v1"
        llm = self.data["llm"]
        uses_gateway = llm["kind"] == "openai_compatible" and llm["base_url"].rstrip("/") == self.omni_base
        if uses_gateway:
            configure_omni_llm(llm)
            self.data.setdefault("responses", responses_profile(root))
        if self.gateway_key or uses_gateway or self._uses_gateway_vision() or self._uses_gateway_responses():
            validate_api_key(self.gateway_key)

    def provider_key(self, name):
        # Diagnostics and hot config reload use this method too. Do not let a
        # saved address change send the service-managed key to another endpoint.
        if name == "responses" and self._uses_gateway_responses():
            return self.gateway_key
        if (
            name == "llm"
            and self.data["llm"]["kind"] == "openai_compatible"
            and self.data["llm"]["base_url"].rstrip("/") == self.omni_base
            and self.gateway_key
        ):
            return self.gateway_key
        return super().provider_key(name)

    def _uses_gateway_vision(self):
        vision = self.data.get("video_vision", {})
        return (
            vision.get("enabled")
            and vision.get("adapter") == "omni"
            and vision.get("base_url", "").rstrip("/") == self.omni_base
        )

    def _uses_gateway_responses(self):
        return self.data.get("responses", {}).get("base_url", "").rstrip("/") == self.responses_base

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
        tts = settings.data["tts"]
        tts_key = settings.broker_key_for(tts["base_url"]) if tts["kind"] == "gpt_sovits" else None
        configure_model(settings.data, omni=True)
        if tts_key and tts_key != settings.provider_key("tts"):
            # The unchanged TTS route still uses its paired credential after the
            # legacy model broker is disabled. The gateway key remains LLM-only.
            validate_api_key(tts_key)
            settings.save_keys(settings.provider_keys | {"tts": tts_key})
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
        log_config = deepcopy(LOGGING_CONFIG)
        log_config["loggers"]["tuber_studio.agent.operator_chat"] = {
            "handlers": ["default"], "level": "INFO", "propagate": False,
        }
        uvicorn.run(build_app, factory=True, host="0.0.0.0", port=9020, ws="wsproto", log_config=log_config)
