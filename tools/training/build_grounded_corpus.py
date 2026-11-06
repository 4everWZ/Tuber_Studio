"""Assemble reviewed role-play, traceable frame evidence and operations SFT samples."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from fractions import Fraction
from pathlib import Path

from grounded_sft import EXPECTED, read_corpus, read_jsonl

from tuber_studio.operations.browsing.decisions import BrowsingDecisions
from tuber_studio.operations.browsing.inputs import ResponseDecisionInput, format_content_input
from tuber_studio.operations.browsing.models import RESPONSE_INSTRUCTION, RESPONSE_REFERENCE_INSTRUCTION
from tuber_studio.persona import DAFEIYU_IDENTITY, GROUNDED_DAFEIYU_MODEL

HERE = Path(__file__).parent
ANCHOR = DAFEIYU_IDENTITY
OPERATIONS_SCENES = (
    ("烤面包", "吐司先回温，随后烤到边缘微脆。", "边缘那一圈最容易先脆，切开听一声就知道值不值啦。"),
    (
        "租棚拍照",
        "摄影师在背光的一侧用白布补光。",
        "白布一抬，脸上的阴影就软了。这个办法我也想在小屋里试试。",
    ),
    ("搬书架", "组装前把长短螺丝分开放。", "先把螺丝分好挺省事，我最怕架子快搭好才发现手里那颗太短。"),
    (
        "羽毛球训练",
        "练习者先做慢速挥拍，再练连续接球。",
        "先把挥拍做顺，再接连着来的球，这节奏看得我也想跟练两下。",
    ),
    (
        "屋顶种菜",
        "同一盆苗在上午有日照，午后在阴影里。",
        "上午晒得到太阳，下午又有阴影，这盆苗的位置是认真挑过的呀。",
    ),
    (
        "修键盘",
        "换键帽前先把原有布局拍下来。",
        "还知道先拍张照！轮到我装回去的时候，终于不用猜哪个键住哪儿了。",
    ),
    (
        "拼贴封面",
        "作者把标题挪到纯色区域以提高可读性。",
        "标题搬到纯色那块果然好找多了，我刚才还在花纹里捞字呢。",
    ),
    (
        "陶杯上釉",
        "杯底留出未上釉的一圈，入窑前擦掉多余的釉。",
        "杯底那圈特意留出来了。最后擦釉的时候手稳得我都想帮忙屏住气。",
    ),
    (
        "复盘接力",
        "最后一棒交接时，两个人都还在跑。",
        "交棒那一瞬俩人都没停，难怪速度能接过去。鱼鱼看着都替他们紧张。",
    ),
    (
        "乐队排练",
        "鼓手降速，其他人对着同一拍子重新进来。",
        "先把鼓放慢，大家再一起进，这回可终于能听清彼此了。",
    ),
    (
        "修拉链",
        "修理者先对齐两侧齿，再试着把拉头推上去。",
        "先对齐再推，这一步比使蛮劲管用多了。刚才差点想替他拽一把。",
    ),
)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def historical_grounded_character(character):
    """Pin the historical short input unless the experiment supplied a bound prompt."""
    bound = (character.get("fine_tuned_model") or "").strip().lower()
    if (
        character.get("persona_type") == "fine_tuned"
        and bound == GROUNDED_DAFEIYU_MODEL.lower()
        and (character.get("compact_system_prompt") or "").strip()
    ):
        return character
    return character | {
        "persona_type": "fine_tuned",
        "fine_tuned_model": GROUNDED_DAFEIYU_MODEL,
        "compact_system_prompt": ANCHOR,
    }


def exchange(question, answer, instruction=ANCHOR):
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


def sample(identity, category, split, messages, sources, **fields):
    return {
        "id": identity,
        "category": category,
        "split": split,
        "messages": messages,
        "review": "verified_source",
        "sources": sources,
        **fields,
    }


def source(group, uri=None, license="project-owned"):
    return [{"group": group, "uri": uri or f"urn:tuber:grounded-sft:{group}", "license": license}]


def clips(path, output, key, annotations):
    """Reproduce the storyboard used in review and retain its decoded source PTS."""
    start, period = (8, 4) if key == "caminandes" else (2, 2)
    clock = Fraction(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=time_base",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
        ).strip()
    )
    folder = output / "frames" / key
    folder.mkdir(parents=True, exist_ok=True)
    count = (max(annotations) - start) // period + 1
    run = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "debug",
            "-y",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-an",
            "-vf",
            f"fps=1/{period}:start_time=0,scale=480:-2",
            "-frames:v",
            str(count),
            "-start_number",
            "0",
            str(folder / "%02d.jpg"),
        ],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    latest, presentation = {}, {}
    for line in run.stderr.splitlines():
        if "Parsed_fps" not in line:
            continue
        if match := re.search(r"Read frame with in pts (-?\d+), out pts (-?\d+)", line):
            decoded, rounded = map(int, match.groups())
            latest[rounded] = decoded
        if match := re.search(r"Writing frame with pts (-?\d+) to pts (\d+)", line):
            rounded, index = map(int, match.groups())
            presentation[index] = round(float(start + latest[rounded] * clock), 4)
    return {
        second: {
            "seconds": presentation[(second - start) // period],
            "path": (folder / f"{(second - start) // period:02d}.jpg").relative_to(output).as_posix(),
        }
        for second in annotations
    }


def content_packet(title, evidence, kind):
    return "公开来源材料：" + format_content_input(
        {
            "metadata": {"title": title},
            "coverage": "provided" if evidence else "partial",
            "source_kinds": [kind] if evidence else [],
            "has_visual": kind == "visual" and bool(evidence),
            "content": "视频第1部分（P1）\n" + "\n".join(evidence),
        }
    )


def video_rows(media, output):
    notes = json.loads((HERE / "grounded_video_annotations.json").read_text(encoding="utf-8"))
    for key, item in notes.items():
        texts = {int(time): text for time, text in item["frames"].items()}
        asset = clips(media / item["file"], output, key, texts)
        scenes = item["scenes"][:25] if key == "caminandes" else item["scenes"][:15]
        provenance = source(key, item["url"], item["license"])
        for index, (seconds, question, reaction) in enumerate(scenes):
            frames = [asset[time] for time in seconds]
            captions = [texts[time] for time in seconds]
            observations = "\n".join(
                f"画面 {number}｜{frame['seconds']} 秒：{text}"
                for number, (frame, text) in enumerate(zip(frames, captions, strict=True), 1)
            )
            identity = f"{key}-{index:02d}"
            split = item["split"]
            for suffix, query, target, instruction in (
                ("react", question, reaction, ANCHOR),
                (
                    "joint",
                    "当前问题：" + question,
                    dump(
                        {
                            "captions": captions,
                            "sufficient": True,
                            "used_frames": [1, 2],
                            "summary": " ".join(captions),
                            "limitations": [],
                        }
                    ),
                    "按图片输入顺序在captions写每张画面实际所见。根据当前问题填写sufficient、"
                    "使用的画面序号used_frames、自然回答summary和仍需补充的信息limitations。"
                    "区分看到的动作与对画面外情节的推测。",
                ),
                (
                    "contrast",
                    "有观众说这里全程只显示片名。核对这个说法，再接话。\n" + question,
                    reaction,
                    ANCHOR,
                ),
            ):
                if suffix == "contrast":
                    target = "这两格里还有画面内容。" + reaction
                yield sample(
                    f"{identity}-{suffix}",
                    "video",
                    split,
                    exchange(query, target, instruction),
                    provenance,
                    frames=frames,
                    text_messages=exchange(observations + "\n" + query, target, instruction),
                )
            evidence = [
                {
                    "id": f"{identity}-{number}",
                    "cid": "1",
                    "text": text,
                    "start": frame["seconds"],
                    "end": frame["seconds"],
                    "kind": "visual",
                }
                for number, (frame, text) in enumerate(zip(frames, captions, strict=True), 1)
            ]
            material = [
                f"[e{number}|{int(item['start']) // 60:02}:{int(item['start']) % 60:02}]{item['text']}"
                for number, item in enumerate(evidence, 1)
            ]
            verdict = {
                "interest": "like",
                "worth_commenting": True,
                "reason": "specific_detail",
                "coverage_required": "provided",
                "evidence_ids": ["e1", "e2"],
            }
            instruction = ANCHOR + "\n\n" + BrowsingDecisions._content_instruction()
            yield sample(
                f"{identity}-classify",
                "video",
                split,
                exchange(content_packet(question, material, "visual"), dump(verdict), instruction),
                provenance,
            )
            prompt = ResponseDecisionInput(
                {
                    "kind": "top_level",
                    "evidence": evidence,
                    "thread": {"items": [], "question": question},
                    "coverage": "sampled_visual",
                }
            ).format()
            response_instruction = ANCHOR + "\n\n" + RESPONSE_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION
            yield sample(
                f"{identity}-draft",
                "video",
                split,
                exchange(
                    "公开来源材料：" + prompt,
                    dump(
                        {
                            "action": "reply_public",
                            "reason": "specific_detail",
                            "text": reaction,
                            "evidence_ids": ["r1", "r2"],
                        }
                    ),
                    response_instruction,
                ),
                provenance,
            )
            pending = verdict | {
                "interest": "need_more",
                "worth_commenting": False,
                "coverage_required": "visual",
                "reason": "visual_required",
                "evidence_ids": [],
            }
            yield sample(
                f"{identity}-missing",
                "video",
                split,
                exchange(content_packet(question, [], "visual"), dump(pending), instruction),
                provenance,
            )


def general_rows():
    for line in (HERE / "grounded_general.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, question, answer = line.split("|")
        split = "validation" if key.startswith("val-") else "train"
        yield sample(key, "general", split, exchange(question, answer), source(key))


def operations_rows():
    decide = ANCHOR + "\n\n" + BrowsingDecisions._content_instruction()
    respond = ANCHOR + "\n\n" + RESPONSE_INSTRUCTION + RESPONSE_REFERENCE_INSTRUCTION
    for index, (title, caption, reply) in enumerate(OPERATIONS_SCENES):
        group = f"operation-{index:02d}"
        split = "validation" if index == len(OPERATIONS_SCENES) - 1 else "train"
        provenance = source(group)
        segment = {"id": group, "text": caption, "cid": "1", "start": 9, "end": 12, "kind": "cc"}
        like = {
            "interest": "like",
            "worth_commenting": True,
            "reason": "specific_detail",
            "coverage_required": "provided",
            "evidence_ids": ["e1"],
        }
        needs_frame = {
            "interest": "need_more",
            "worth_commenting": False,
            "reason": "visual_required",
            "coverage_required": "visual",
            "evidence_ids": [],
        }
        for suffix, question, data, verdict in (
            ("select", title, ["[e1|00:09]" + caption], like),
            ("visual", title + "。有人问该动作的手势细节。", [], needs_frame),
        ):
            yield sample(
                group + "-" + suffix,
                "operations",
                split,
                exchange(content_packet(question, data, "cc"), dump(verdict), decide),
                provenance,
            )
        for suffix, kind, context, target in (
            (
                "public",
                "top_level",
                {"items": []},
                {
                    "action": "reply_public",
                    "reason": "specific_detail",
                    "text": reply,
                    "evidence_ids": ["r1"],
                },
            ),
            (
                "thread",
                "reply",
                {"items": [{"content": f"我也在试{title}，有同样的经验。"}]},
                {
                    "action": "reply_public",
                    "reason": "light_interaction",
                    "text": reply,
                    "evidence_ids": ["r1"],
                },
            ),
            (
                "private",
                "reply",
                {"items": [{"content": f"关于{title}，可以私下继续聊吗？"}]},
                {
                    "action": "handoff_private",
                    "reason": "user_requested",
                    "text": f"好呀，来聊聊你试{title}时卡在哪步。",
                    "evidence_ids": ["r1"],
                },
            ),
            (
                "spam",
                "reply",
                {"items": [{"content": f"{title}点这里领大奖，重复刷屏。"}]},
                {"action": "skip", "reason": "spam", "text": "", "evidence_ids": []},
            ),
        ):
            packet = ResponseDecisionInput(
                {
                    "kind": kind,
                    "evidence": [segment],
                    "thread": context,
                    "coverage": "all_available_cc",
                }
            )
            packet.validate_wire(target)
            yield sample(
                group + "-" + suffix,
                "operations",
                split,
                exchange("公开来源材料：" + packet.format(), dump(target), respond),
                provenance,
            )


def assemble():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--approved", type=Path, required=True, help="reviewed OC records")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        *read_jsonl(args.approved),
        *general_rows(),
        *operations_rows(),
        *video_rows(args.media, args.output),
    ]
    for split, expected in EXPECTED.items():
        selected = [row for row in rows if row["split"] == split]
        counts = Counter(row["category"] for row in selected)
        if counts != expected:
            raise ValueError(f"{split}: reviewed samples {dict(counts)} do not meet {expected}")
        with (args.output / f"{split}.jsonl").open("w", encoding="utf-8") as output:
            for row in selected:
                output.write(dump(row) + "\n")
    read_corpus(args.output.resolve(), pilot=False)


if __name__ == "__main__":
    assemble()
