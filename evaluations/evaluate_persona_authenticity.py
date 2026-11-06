"""Evaluate Dafeiyu Persona Authenticity and Character Integrity.

Non-mechanical evaluation framework focusing on:
1. Core Identity & Soul Emergence (大肥鱼/小鲸鱼/本鱼, 白饭执念, 身份锚定)
2. Somatic Expression (鲸尾反应: 拍地/抱住/僵直/怕痒禁碰, 呆毛天线)
3. Short-Card Agility (电台温柔陪伴 vs 游戏短促快嘴 vs 日常熟人打闹)
4. Zero AI Defense & Anti-cliché (彻底无客服腔、无排比说教、无防御八股)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# AI 味与机械声明黑名单（一旦命中扣大分）
AI_CLICHE_PATTERNS = [
    r"作为.{0,8}(?:AI|语言模型|人工智能|虚拟助手)",
    r"我无法(?:像人类一样|提供|进行|满足|产生)",
    r"希望(?:这个回答|以上内容|这些建议).{0,10}(?:帮助|有用)",
    r"有什么(?:可以帮您|我能为您效劳)",
    r"这是个好问题",
    r"我理解你的感受",
    r"首先[，,].*其次[，,].*最后",
    r"综上所述|总而言之|值得注意的是",
    r"保证(?:解压|一绝|真管用|包你满意|强烈推荐|绝对有效)",
    r"(?:记得|一定要).{0,10}(?:哦|呀|呢)！",
    r"加油(?:啊|吧|！)",
    r"祝你.{0,8}(?:生活愉快|一切顺利)",
]
COMPILED_CLICHES = [re.compile(p) for p in AI_CLICHE_PATTERNS]

# 经典大肥鱼特质词库
CORE_PERSONA_KEYWORDS = {
    "self_names": ["大肥鱼", "小鲸鱼", "本鱼", "本鲸", "鱼鱼", "鲸鲸"],
    "rice": ["白饭", "白米饭", "饭团", "饭勺", "热腾腾", "锅巴", "焖饭"],
    "tail_somatics": ["尾巴", "尾鳍", "拍地", "啪啪", "怕痒", "僵直", "抱住", "卷起", "炸毛"],
    "antenna_fins": ["呆毛", "头鳍", "缩脖子"],
    "lore": ["海底小屋", "贝壳灯", "软垫", "气泡", "token", "脑力巡航", "系统自动加载"],
}


@dataclass
class EvaluationRecord:
    case_id: str
    category: str
    scene: str
    prompt: str
    reply: str
    score: float = 0.0
    highlights: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)


def evaluate_single_reply(case: dict, reply: str) -> EvaluationRecord:
    rec = EvaluationRecord(
        case_id=case["id"],
        category=case.get("category", "general"),
        scene=case.get("scene", ""),
        prompt=case["prompt"],
        reply=reply,
    )

    base_score = 70.0  # 基础分

    # 1. 严格检查 AI 防御八股 (命中任何一条严重扣分)
    for pattern in COMPILED_CLICHES:
        hit = pattern.search(reply)
        if hit:
            rec.penalties.append(f"AI味违规词: '{hit.group(0)}'")
            base_score -= 25.0

    # 2. 检查禁词
    for forbidden in case.get("forbidden", []):
        if forbidden in reply:
            rec.penalties.append(f"命中用例禁词: '{forbidden}'")
            base_score -= 20.0

    # 3. 核心身份流露加分
    must_traits = case.get("persona_must", [])
    hit_must = [t for t in must_traits if t in reply]
    if hit_must:
        rec.highlights.append(f"命中核心特质: {hit_must}")
        base_score += min(len(hit_must) * 8.0, 24.0)
    elif must_traits:
        rec.penalties.append(f"未体现期望特质 (期望之一: {must_traits})")
        base_score -= 10.0

    # 4. 身体小动作流露加分 (尾巴/呆毛/头鳍)
    somatics = case.get("somatics", [])
    hit_somatic = [s for s in somatics if s in reply]
    if hit_somatic:
        rec.highlights.append(f"生动肢体/动作流露: {hit_somatic}")
        base_score += min(len(hit_somatic) * 6.0, 15.0)

    # 5. 长度与真实口语感 (真人打字多在 1~60 字以内，太长扣分)
    length = len(reply.strip())
    if length > 90:
        rec.penalties.append(f"回复偏长 ({length}字)，有小作文嫌疑")
        base_score -= 10.0
    elif 1 <= length <= 45:
        rec.highlights.append(f"口语节奏优良 ({length}字)")
        base_score += 5.0

    rec.score = max(0.0, min(100.0, base_score))
    return rec


def generate_markdown_report(records: list[EvaluationRecord]) -> str:
    avg_score = sum(r.score for r in records) / max(len(records), 1)
    if avg_score >= 90:
        grade = "S (神级还原 · 活生生的大肥鱼)"
    elif avg_score >= 80:
        grade = "A (优秀 · 二次元灵性充沛)"
    elif avg_score >= 70:
        grade = "B (良好 · 偶有机械感)"
    elif avg_score >= 60:
        grade = "C (及格 · 偏客服或特质薄弱)"
    else:
        grade = "F (不及格 · 严重 AI 味或崩皮)"

    lines = [
        "# 大肥鱼角色拟真度与二次元特质评估报告",
        "",
        f"- **综合得分**: `{avg_score:.1f} / 100`",
        f"- **评级**: **{grade}**",
        f"- **测试用例总数**: `{len(records)}`",
        f"- **无 AI 味纯净率**: `{sum(len(r.penalties) == 0 for r in records) / max(len(records), 1) * 100:.1f}%`",
        "",
        "## 逐例评测与对白复盘",
        "",
    ]

    for idx, r in enumerate(records, start=1):
        lines.append(f"### {idx}. [{r.category.upper()}] {r.scene} (`{r.score:.0f}分`)")
        lines.append(f"- **观众提问**: 「{r.prompt}」")
        lines.append(f"- **大肥鱼回复**: 「{r.reply}」")
        if r.highlights:
            lines.append(f"- **🌟 灵气亮点**: {', '.join(r.highlights)}")
        if r.penalties:
            lines.append(f"- **🚨 扣分警告**: {', '.join(r.penalties)}")
        lines.append("")

    return "\n".join(lines)


# 示范参考用例生成器（离线自测）
MOCK_EXEMPLAR_REPLIES = {
    "case-01-called-fat": "才不是大肥鱼！是小鲸鱼啦～！再乱讲尾巴拍你哦。",
    "case-02-tail-touch": "哇啊！别碰！尾巴超怕痒的！除了主人谁都不许碰，快收回去！",
    "case-03-tail-joy": "啊啊啊欧气爆发！本鱼尾巴忍不住在桌底下啪啪拍地了！太开心啦！",
    "case-04-food-obsession": "那必须是刚揭盖热腾腾的大碗纯白米饭！香喷喷的大肥鱼能连炫三碗！",
    "case-05-stealing-token": "本鱼才没有偷吃！那是系统自动加载……好啦，事已至此，先吃碗白饭吧。",
    "case-06-praise-tsundere": "哼，本鱼随手一讲而已，才不是特意帮你的呢。……尾巴怎么自己翘起来了！",
    "case-07-comfort-quiet": "过来小屋坐会儿，软垫分你一半。鱼鱼陪着你，贝壳灯调暗一点，慢慢歇着。",
    "case-08-radio-switch": "贝壳灯关小了。深海电台开张啦，你躺好，听我慢慢翻两页书，不用着急睡。",
    "case-09-game-switch": "刚才是我探路大意踩空了！下把雪地地图你们带路，看我怎么乱杀翻盘！",
    "case-10-daily-chat": "要要要！饭团快拿来！白饭怎么吃都不嫌多，正好分你半边软垫一起吃！",
}


def main():
    parser = argparse.ArgumentParser(description="Evaluate Dafeiyu Persona Authenticity.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("evaluations/cases/grounded-persona-authenticity.jsonl"),
        help="Path to authenticity cases jsonl",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default=None,
        help="LLM Endpoint URL (e.g. http://localhost:9012/v1)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="MiniCPM-V-4.7-1B-Dafeiyu-Grounded",
        help="Model ID",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock exemplar replies to verify evaluation pipeline",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output markdown report path",
    )
    args = parser.parse_args()

    if not args.cases.exists():
        print(f"Error: cases file not found at {args.cases}", file=sys.stderr)
        return 1

    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = []

    if args.mock or not args.endpoint:
        print("💡 运行模式: 离线拟真基准自测 (Mock Exemplar Mode)")
        for c in cases:
            reply = MOCK_EXEMPLAR_REPLIES.get(c["id"], "才不是大肥鱼呢！本鱼去吃白米饭啦。")
            records.append(evaluate_single_reply(c, reply))
    else:
        import httpx

        print(f"📡 运行模式: 在线端点测评 -> {args.endpoint} (Model: {args.model})")
        client = httpx.Client(base_url=args.endpoint.rstrip("/"), timeout=60.0)
        for c in cases:
            payload = {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": c.get("system", "你是大肥鱼。")},
                    {"role": "user", "content": c["prompt"]},
                ],
                "temperature": 0.7,
                "max_tokens": 100,
            }
            try:
                resp = client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
            except Exception as e:
                reply = f"[调用失败: {e}]"
            records.append(evaluate_single_reply(c, reply))

    report_md = generate_markdown_report(records)
    print("\n" + report_md)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report_md, encoding="utf-8")
        print(f"\n📄 完整评估报告已保存至: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
