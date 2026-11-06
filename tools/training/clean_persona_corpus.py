"""Screen and clean persona corpus to eliminate AI defense disclaimers and mechanical patterns."""

import argparse
import json
import re
import sys
from pathlib import Path

# 严格的 AI 八股、机械说教、防御声明与口号式黑名单
AI_DEFENSE_PATTERNS = [
    r"作为.{0,8}(?:AI|语言模型|人工智能|虚拟助手)",
    r"作为一个.{0,8}(?:AI|语言模型|助手)",
    r"我无法(?:像人类一样|提供|进行|满足|产生)",
    r"希望(?:这个回答|以上内容|这些建议).{0,10}(?:对你|对您).{0,10}(?:帮助|有用|参考)",
    r"有什么(?:可以帮您|我能为您效劳|我能帮你的)",
    r"随时找我|请随时提问",
    r"这是个好问题",
    r"我理解你的感受",
    r"首先[，,].*其次[，,].*最后",
    r"综上所述|总而言之|值得注意的是|不可否认的是",
    r"保证(?:解压|一绝|真管用|刺激多了|包你满意|强烈推荐|绝对有效)",
    r"(?:记得|一定要).{0,10}(?:哦|呀|呢)！",
    r"加油(?:啊|吧|！)",
    r"祝你.{0,8}(?:生活愉快|一切顺利|前程似锦)",
]

COMPILED_PATTERNS = [re.compile(p) for p in AI_DEFENSE_PATTERNS]

DAFEIYU_ESSENCE_WORDS = [
    "大肥鱼",
    "小鲸鱼",
    "本鱼",
    "鱼鱼",
    "白饭",
    "尾巴",
    "尾鳍",
    "呆毛",
    "海底",
    "软垫",
    "贝壳灯",
]


def check_text(text: str) -> list[str]:
    """Return all matched defense patterns in text."""
    hits = []
    for pattern in COMPILED_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(match.group(0))
    return hits


def audit_corpus_file(path: Path) -> dict:
    """Audit a txt or jsonl file for AI defense disclaimers."""
    results = {"path": str(path), "total_lines": 0, "violations": []}

    if not path.exists():
        results["error"] = "File not found"
        return results

    content = path.read_text(encoding="utf-8")

    if path.suffix == ".jsonl":
        for idx, line in enumerate(content.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            results["total_lines"] += 1
            try:
                data = json.loads(line)
                # Check messages in row
                text_to_check = json.dumps(data, ensure_ascii=False)
                hits = check_text(text_to_check)
                if hits:
                    results["violations"].append({"line": idx, "id": data.get("id", "unknown"), "hits": hits})
            except Exception as e:
                results["violations"].append({"line": idx, "error": str(e)})
    else:
        for idx, line in enumerate(content.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            results["total_lines"] += 1
            hits = check_text(line)
            if hits:
                results["violations"].append({"line": idx, "preview": line[:60], "hits": hits})

    return results


def main():
    parser = argparse.ArgumentParser(description="Audit training corpus for AI defensive disclaimers.")
    parser.add_argument("--file", type=Path, help="Specific file to audit", default=None)
    parser.add_argument(
        "--dir",
        type=Path,
        help="Directory with training files",
        default=Path(__file__).parent,
    )
    args = parser.parse_args()

    files_to_check = []
    if args.file:
        files_to_check.append(args.file)
    else:
        for ext in ("*.txt", "*.jsonl"):
            files_to_check.extend(args.dir.glob(ext))

    total_violations = 0
    print("=== Tuber Studio · 大肥鱼训练语料去防御检查 ===")
    for f in sorted(files_to_check):
        res = audit_corpus_file(f)
        v_count = len(res.get("violations", []))
        total_violations += v_count
        status = "✅ CLEAN" if v_count == 0 else f"❌ {v_count} VIOLATIONS"
        print(f"[{status}] {f.name} ({res['total_lines']} items)")
        if v_count > 0:
            for v in res["violations"][:5]:
                print(f"   Line {v.get('line')}: hits={v.get('hits')}")

    print("\n--- 检查结果 ---")
    if total_violations == 0:
        print("🎉 全部语料完全纯净，未发现任何机械防御声明与 AI 八股！")
        return 0
    else:
        print(f"⚠️ 发现 {total_violations} 处需清洗的防御性表述。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
