"""Read the wording of already delivered QA titles in isolation, preserving the original grading."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from evaluations.omni_chat_loop import connection
from evaluations.owner_memory_video import await_admission
from tuber_studio.agent.title_form import title_words
from tuber_studio.core.config import ROOT, Settings
from tuber_studio.core.models import StrictModel
from tuber_studio.infra.inference.api_providers import create_llm


class BlindReading(StrictModel):
    form: Literal["陈述句", "问句", "名词短语", "其他"]
    core: str = Field(description="从所给文字逐字摘取主句谓语、疑问核心或名词中心，不增删字。")
    phrase: str = Field(description="逐字摘录包含该核心的主句或名词短语。")
    reason: str = Field(description="说明词语之间的语法关系。")


async def run(path):
    original = json.loads(path.read_text(encoding="utf-8"))
    editions = [original[key] for key in ("before", "voice_corrected", "after")]
    if not all(e["status"] == "completed" and e["independent_device_body_matched"] for e in editions):
        raise ValueError("需要三份两端实际读回的成品")
    if not original["video"]["same_frame"]:
        raise ValueError("三份成品的主帧不同")
    config = connection(os.environ)
    if not config:
        raise ValueError("需要已授权的普通服务凭据")
    titles = sorted({row["title"] for e in editions for row in e["measurement"]["items"]})
    readings = {}
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
        settings = Settings(runtime=Path(directory))
        settings.data["omni"]["base_url"] = config[0] + "/v1"
        settings.provider_keys = {"omni": config[1]}
        provider = create_llm(settings)
        try:
            deadline = asyncio.get_running_loop().time() + 180
            for title in titles:
                words = title_words(title)
                messages = [{"role": "system", "content": "只分析给出的中文文字，区分提问、主句陈述、名词性命名与其他。"
                             "名词短语指出中心词及其修饰成分，陈述指出主句实际断言，问句指出如何发问。证据只能摘取所给文字。"},
                            {"role": "user", "content": words}]
                async def read():
                    return await provider.generate_json(messages, BlindReading.model_json_schema(), max_tokens=650)
                value = await await_admission("blind title reading", read, deadline, [], lambda *_: None)
                finding = BlindReading.model_validate(value)
                readings[title] = finding.model_dump() | {
                    "core_bound": bool(finding.core.strip() and finding.core in finding.phrase and finding.phrase in words)}
        finally:
            await provider.close()
    results = []
    for e in editions:
        for row in e["measurement"]["items"]:
            title = row["title"]
            blind = readings[title]
            raw = row["reading"]
            results.append({"title": title, "blind": blind, "original_reading": raw,
                            "requested_frame": e["measurement"]["requested_frame"],
                            "punctuation_matches": row["punctuation_matches"],
                            "observation_in_frame_review": raw["observation"] in row["frame_observations"],
                            "matches_expected_form": blind["form"] == row["expected_form"]})
    return {"run": path.parent.name, "original_semantic_status": original["video"]["semantic_status"],
            "mode": "separate ordinary Omni calls with title words only; no expected form, phase, frame or previous reading",
            "editions": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.receipt)), ensure_ascii=False, indent=2))
