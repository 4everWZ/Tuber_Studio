"""Start the normal Tuber Studio workbench with a deployment-scoped Omni key."""

import argparse
import os
from copy import deepcopy
from pathlib import Path

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from tuber_studio.app import create_app
from tuber_studio.core.authority import acquire_business_writer, read_business_authority
from tuber_studio.core.config import Settings, omni_base
from tuber_studio.core.validation import validate_api_key


def deployed_base():
    return omni_base(os.getenv("OMNI_GATEWAY_URL", "http://127.0.0.1:8000"))


def configure_gateway(settings):
    profile = settings.data["omni"]
    model = os.getenv("OMNI_MODEL_ALIAS", "omni-default")
    background = os.getenv("OMNI_LLM_MODEL_ALIAS", "llm-default")
    if (profile["base_url"], profile["responses_model"]) != (deployed_base(), background):
        profile.get("video_options", {}).pop("background_model_revision", None)
    profile.update(base_url=deployed_base(), model=model, responses_model=background)
    if pin := os.getenv("LLM_VISION_CHECKPOINT"):
        profile.setdefault("video_options", {})["background_model_revision"] = pin


class ComposeSettings(Settings):
    def __init__(self):
        super().__init__()
        mode = os.environ.get("TUBER_CORE_MODE", "omni")
        if mode not in {"omni", "existing"}:
            raise ValueError("TUBER_CORE_MODE must be omni or existing")
        if mode == "existing" and not self.path.is_file():
            raise ValueError("An existing Core volume needs its studio.json")
        self.deployed_base = deployed_base()
        key_file = os.environ.get("OMNI_GATEWAY_TOKEN_FILE")
        self.gateway_key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else None
        if self.gateway_key is not None:
            validate_api_key(self.gateway_key)
        new = not self.path.exists()
        if new:
            configure_gateway(self)
        if self.data["omni"]["base_url"] == self.deployed_base and not self.provider_key("omni"):
            raise ValueError("Core needs a credential for the configured Omni gateway")
        if new:
            self.save_public(self.data)

    def provider_key(self, name):
        saved = super().provider_key(name)
        if not saved and self.data["omni"]["base_url"] == self.deployed_base:
            return self.gateway_key
        return saved


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
        configure_gateway(settings)
        settings.save_connection(settings.data, settings.provider_keys if settings.provider_key("omni") else {})


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
        log_config["loggers"]["tuber_studio.agent.chat.service"] = {
            "handlers": ["default"], "level": "INFO", "propagate": False,
        }
        uvicorn.run(build_app, factory=True, host="0.0.0.0", port=9020, ws="wsproto", log_config=log_config)
