"""Mix human anime roleplay, Dafeiyu core and variants, and capability replay."""

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from augment_grounded_persona import authored
from build_dafeiyu_first_person import dialogue_rows
from build_grounded_corpus import dump, exchange, historical_grounded_character, sample, source
from draft_oc_dialogues import CHATANIME_SOURCE
from grounded_sft import ANIME_DAFEIYU_EXPECTED, FIRST_PERSON_EXPECTED, read_corpus

STYLE_CARDS = {
    "radio": "你是大肥鱼。今晚是海底电台，声音放轻，给来的人留点安静。",
    "game": "你是大肥鱼。开黑时随口接梗，输了认账，正事报清楚。",
    "daily": "你是大肥鱼。熟人来小屋串门，像平常一样随口聊。",
}
CLASSIC_STYLES = {
    "蒙奇·D·路飞": "直来直去，重伙伴，遇事愿意先动起来。",
    "漩涡鸣人": "有冲劲，容易把眼前的事变成和朋友一起练习。",
    "坂田银时": "懒散，嘴上嫌麻烦，遇到朋友真有事会搭把手。",
    "夏目贵志": "温和安静，会留意别人不方便说出口的事。",
    "桐谷和人": "话不多，愿意陪对方一起试，遇事会看眼前怎么做。",
    "折木奉太郎": "惜力，观察细，能一句说明白就不会多说。",
    "利威尔·阿克曼": "直接，话少，有事说清楚。关心经常落在实际行动上。",
    "日向翔阳": "直爽，做成一件小事也会兴奋，愿意拉着队友再练。",
    "中原中也": "脾气来得快，有自己的立场，认同对方时也不遮掩。",
    "五条悟": "有点张扬，爱逗人，必要时把事情讲清。",
    "逢坂大河": "容易炸毛，嘴硬，关心人时动作比嘴快。",
    "御坂美琴": "直爽爱较劲，认真起来会帮忙，被当面夸会别扭。",
    "牧濑红莉栖": "敏锐爱抬杠，重证据，真心夸奖会让她不自在。",
    "Saber（阿尔托莉雅·潘德拉贡）": "认真有担当，礼貌而直接，答应的事会记在心里。",
    "鹿目圆": "温柔，愿意听对方说完，急事也会拿定主意。",
    "五更琉璃": "说话有戏，偶尔中二，被看穿会嘴硬。",
}
NOVEL_STYLES = {
    "结城明日奈（亚丝娜）": "做事利索，体贴同伴，有问题会先一起解决。",
    "加藤惠": "声调平平，善于接住小细节，吐槽往往说得很轻。",
    "蕾姆": "平时礼貌，表达关心时愿意给具体帮助。",
    "薇尔莉特·伊芙加登": "说话认真克制，听到不明白的感受会请对方讲具体一点。",
}
EDITORIAL_SCREEN = re.compile(
    r"群友|群里|群聊|QQ群|作为.{0,8}(?:AI|语言模型)|首先|其次|综上所述|总而言之|"
    r"希望.{0,10}(?:帮助|有用)|加油(?:啊|吧|！)|(?:别担心|没关系).{0,12}(?:相信|一定可以)"
)
CLASSIC_PER_ROLE = 14


def eligible(row):
    values = [row[f"{kind}_r{turn}"].strip() for turn in (1, 2) for kind in ("question", "answer")]
    if any(EDITORIAL_SCREEN.search(text) for text in values):
        return False
    a, b = values[1], values[3]
    return 4 <= len(a) <= 125 and 4 <= len(b) <= 125 and a != b


def select_classic(human, seen_groups):
    usage = Counter()
    for role, style in CLASSIC_STYLES.items():
        title, rows = next((title, rows) for title, rows in human.items() if title.endswith("的" + role))
        candidates = []
        for row in rows:
            group = f"chatanime-{int(row['dialog_id']):02d}"
            if group in seen_groups and eligible(row):
                candidates.append((group, row))
        if len(candidates) < CLASSIC_PER_ROLE:
            raise ValueError(f"{title}: only {len(candidates)} screened scenes")
        for _ in range(CLASSIC_PER_ROLE):
            group, row = min(candidates, key=lambda pair: (
                usage[pair[0]], len(pair[1]["answer_r1"]) + len(pair[1]["answer_r2"]), pair[0],
            ))
            candidates.remove((group, row))
            usage[group] += 1
            card = f"你是{title}。{style}"
            dialogue = exchange(row["question_r1"].strip(), row["answer_r1"].strip(), card)
            dialogue += exchange(row["question_r2"].strip(), row["answer_r2"].strip())[1:]
            yield sample(
                f"classic-{list(CLASSIC_STYLES).index(role) + 1:02d}-{group}", "oc", "train", dialogue,
                source(group, CHATANIME_SOURCE, "Apache-2.0"), character=role,
            )


def classic_holdout(human, heldout_groups):
    for role, style in NOVEL_STYLES.items():
        title, rows = next((title, rows) for title, rows in human.items() if title.endswith("的" + role))
        chosen = [row for row in rows if f"chatanime-{int(row['dialog_id']):02d}" in heldout_groups and eligible(row)]
        if len(chosen) < 4:
            raise ValueError(f"{title}: too few held-out scenes")
        for row in chosen[:4]:
            yield {
                "id": f"classic-holdout-{role}-{row['dialog_id']}",
                "card": f"你是{title}。{style}",
                "turns": [row["question_r1"], row["question_r2"]],
                "source_group": f"chatanime-{int(row['dialog_id']):02d}",
                "reference": [row["answer_r1"], row["answer_r2"]],
            }


def visual_replay(rows):
    buckets = defaultdict(list)
    for row in rows:
        if row["category"] == "video":
            buckets[(row["sources"][0]["group"], bool(row.get("frames")))].append(row)
    for bucket in buckets.values():
        count = len(bucket) * 3 // 5
        yield from (bucket[index * len(bucket) // count] for index in range(count))


def variant_rows(character):
    for style, name, *fields in authored(Path(__file__).with_name("grounded_dafeiyu_variants.txt")):
        identity = f"dafeiyu-{style}-{name}"
        styled = character | {"compact_system_prompt": STYLE_CARDS[style]}
        yield from dialogue_rows(styled, "live", identity, source(identity), fields)


def write_rows(path, rows):
    path.write_text("".join(dump(row) + "\n" for row in rows), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("original", "output", "characters", "chatanime-human", "holdout"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    data = read_corpus(args.original, pilot=False, expected=FIRST_PERSON_EXPECTED)
    groups = {
        split: {item["group"] for row in rows for item in row["sources"] if item["group"].startswith("chatanime-")}
        for split, rows in data.items()
    }
    human = json.loads(args.chatanime_human.read_text(encoding="utf-8"))
    character = historical_grounded_character(next(
        row for row in json.loads(args.characters.read_text(encoding="utf-8")) if row["id"] == "whale"
    ))
    rows = data["train"]
    retained = [row for row in rows if row["category"] in {"operations", "general"}]
    authored_oc = [row for row in rows if row["id"].startswith(("first-person-", "anime-transfer-"))]
    classics = list(select_classic(human, groups["train"]))
    variants = list(variant_rows(character))
    train = retained + list(visual_replay(rows)) + authored_oc + classics + variants
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(args.original / "frames", args.output / "frames")
    write_rows(args.output / "train.jsonl", train)
    shutil.copy2(args.original / "validation.jsonl", args.output / "validation.jsonl")
    read_corpus(args.output, pilot=False, expected=ANIME_DAFEIYU_EXPECTED)
    write_rows(args.holdout, classic_holdout(human, groups["validation"]))
    print(json.dumps({
        "training": len(train), "classic": len(classics), "classic_characters": len(CLASSIC_STYLES),
        "dafeiyu_core": sum(row["id"].startswith("first-person-") for row in authored_oc),
        "dafeiyu_variants": len(variants), "other_authored_anime": sum(
            row["id"].startswith("anime-transfer-") for row in authored_oc
        ),
        "retention": dict(Counter(row["category"] for row in train if row["category"] != "oc")),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
