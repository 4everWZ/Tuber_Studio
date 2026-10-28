"""Small source-backed memory beside, rather than inside, context compaction.

Extraction intentionally recognizes few past-tense constructions. Other text is
still searchable, but it cannot become a structured fact by assertion alone.
Assistant stories remain fictional even when their original sentences are saved.
"""

import json
import re
import time

SLOTS = ("location", "person", "action", "item", "quantity")
CLAUSE = re.compile(r"[^，,。.!！?？；;\n]+")
FUTURE_OR_NEGATED = re.compile(
    r"下次|下期|下回|明天|后天|以后|将来|计划|准备|打算|希望|想去|要去|"
    r"还没|没有|没去|没送|未去|未送|不去|不送|不是|如果|假如|可能|也许|听说|据说|不确定|记不清|似乎"
)
LOCATION = re.compile(
    r"去(?:了|过)(?P<location>[^，,。.!！?？；;\n]{1,24}?)"
    r"(?=买了|吃了|喝了|遇见|碰到|看见|散步|逛了|然后|$)"
)
NUMBER = r"[0-9零〇一二两三四五六七八九十百]+"
UNIT = r"[份个杯本袋只颗条块盒碗件束瓶张枚包]"
GIFT = re.compile(
    r"(?P<person>[\w\u4e00-\u9fff·]{1,16}?)(?P<action>送(?:给|了)?(?:了)?我|给了我|赠送给我)"
    rf"(?P<quantity>{NUMBER}{UNIT})(?P<item>[^，,。.!！?？；;\n]{{1,24}})$"
)
PERSON_TITLE = re.compile(r"^(?:店主|店长|老板|店员|朋友|同学|邻居|船长|摊主)")


def required_slots(question):
    """Only explicit factual questions impose completeness requirements."""
    if re.search(r"下次|下期|下回|明天|后天|以后|将来|计划|准备|打算|希望|如果|假如", question):
        return []
    patterns = {
        "location": r"哪里|哪儿|何处|什么地方|哪家(?:店|铺)|去了哪|\bwhere\b",
        "person": r"谁|哪位|什么人|\bwho\b",
        "item": r"(?:送|给|赠|得到|收到|拿到).{0,12}(?:什么|啥)|什么(?:东西|礼物|物品)|\bwhat\b",
        "quantity": r"多少|几个|几份|几杯|几本|几袋|几只|数量|\bhow many\b",
    }
    return [slot for slot, pattern in patterns.items() if re.search(pattern, question, re.I)]


def extract_facts(text):
    """Return literal, source-spanned facts for unambiguous occurred clauses."""
    facts = []
    # A future frame can govern following clauses too ("明天……，店主会……").
    if re.search(r"下次|下期|下回|明天|后天|以后|将来|计划|准备|打算|希望|如果|假如", text):
        return facts
    for clause in CLAUSE.finditer(text):
        sentence = clause.group().strip()
        if FUTURE_OR_NEGATED.search(sentence) or re.search(r"谁|什么|哪里|哪儿|多少|几个", sentence):
            continue
        # Require first-person/direct past wording; do not index quoted stories,
        # questions, or a statement about what an unrelated person did as our trip.
        location = LOCATION.search(sentence)
        if location and re.fullmatch(
            r"(?:我(?:们)?|刚才|刚刚|之前|昨天|今天|已经|又|曾经|后来|随后|\s)*", sentence[: location.start()]
        ):
            value = location.group("location").strip()
            if value and not re.search(r"(?:想|说|问|听|会|能|不|没|未)", value):
                facts.append(_fact(text, clause, "location", value))
        gift = GIFT.fullmatch(sentence)
        if gift:
            for slot in ("person", "item", "quantity"):
                value = gift.group(slot)
                if not re.search(r"[“”\"《》]", value):
                    facts.append(_fact(text, clause, slot, value))
            facts.append(_fact(text, clause, "action", gift.group("action")))
    return facts


def _fact(text, clause, slot, value):
    fact = {"slot": slot, "value": value, "quote": clause.group(), "span": [clause.start(), clause.end()]}
    if slot == "person":
        short = PERSON_TITLE.sub("", value)
        if short and short != value:
            fact["aliases"] = [short]
    assert text[fact["span"][0] : fact["span"][1]] == fact["quote"]
    return fact


def _number(value):
    value = value.replace("两", "二").replace("〇", "零")
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    if value in digits:
        return digits[value]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", value):
        left, right = value.split("十")
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    # Unknown number forms are deliberately not used to reject an answer.
    return None


def missing_slots(answer, evidence):
    """Check only requested slots for which retrieved sources offer evidence.

    This is a bounded omission check, not a truth classifier. It does not require
    facts during casual chat, invent facts when retrieval is empty, or equate an
    uncertain quantity expression with a definite failure.
    """
    sources = evidence.get("evidence", [])
    requested = evidence.get("required_slots", [])
    if not sources or not requested:
        return []
    groups = {}
    for source in sources:
        for fact in source.get("facts", []):
            groups.setdefault(fact["slot"], []).append(fact)
    missing = []
    for slot in requested:
        facts = groups.get(slot, [])
        if not facts:
            continue
        if slot == "quantity":
            expected = {_number(re.match(NUMBER, fact["value"]).group()) for fact in facts}
            expected.discard(None)
            observed = {_number(match.group(1)) for match in re.finditer(rf"({NUMBER}){UNIT}", answer)}
            bare_count = re.fullmatch(rf"\s*(?:有|是|一共|总共)?({NUMBER})[。.!！?？\s]*", answer)
            if bare_count:
                observed.add(_number(bare_count.group(1)))
            observed.discard(None)
            if not expected:
                continue
            if expected & observed:
                continue
            # Colloquial/approximate counts are outside the conservative parser.
            if re.search(r"几|多|少|半|一打|一双|一对|俩|仨|数", answer):
                continue
        elif any(value in answer for fact in facts for value in [fact["value"], *fact.get("aliases", [])]):
            continue
        missing.append(slot)
    return missing


def _terms(text):
    terms = set(re.findall(r"[A-Za-z0-9_]{2,}", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms - {"你刚", "刚才", "去了", "哪里", "什么", "了你", "送了", "你的", "你说", "记得", "上次"}


class GroundedMemory:
    def __init__(self, store):
        self.store = store
        store.execute("""CREATE TABLE IF NOT EXISTS grounded_memory(
            id INTEGER PRIMARY KEY,scope TEXT NOT NULL,character_id TEXT NOT NULL,
            character_version INTEGER NOT NULL,event_id TEXT NOT NULL,role TEXT NOT NULL,
            text TEXT NOT NULL,kind TEXT NOT NULL,fictional INTEGER NOT NULL,
            facts TEXT NOT NULL,created_at REAL NOT NULL,
            UNIQUE(scope,character_id,character_version,event_id,role))""")
        store.execute("""CREATE INDEX IF NOT EXISTS grounded_memory_scope
            ON grounded_memory(scope,character_id,character_version,id DESC)""")

    def record(self, scope, character, event_id, role, text, *, kind=None, facts=None):
        """Save an immutable original. Re-recording the same event is idempotent.

        Optional externally proposed facts must both quote the exact source span
        and match this extractor's supported semantics. No candidate can teach
        the store a new fact merely by naming a slot.
        ``kind`` is trusted runtime metadata, never a viewer-controlled field.
        """
        if not all(isinstance(value, str) and value.strip() for value in (scope, event_id, text)):
            raise ValueError("Memory source needs scope, event ID and original text")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported memory source role")
        character_id, version = self._identity(character)
        defaults = {"assistant": "role_story", "user": "viewer_message", "system": "operator_note"}
        kind = kind or defaults[role]
        if kind not in {
            "role_story",
            "role_plan",
            "viewer_message",
            "operator_note",
            "source_quote",
            "derived_reply",
            "grounded_reply",
        }:
            raise ValueError("Unsupported memory source kind")
        extracted = extract_facts(text)
        # Questions/suggestions from viewers cannot establish a character's past.
        if role != "assistant" or kind in {"role_plan", "derived_reply", "grounded_reply"}:
            extracted = []
        if facts is not None:
            if not isinstance(facts, list):
                raise ValueError("Facts must be a list of source-supported candidates")
            selected = []
            for fact in facts:
                required = {"slot", "value", "quote", "span"}
                if not isinstance(fact, dict) or set(fact) not in (required, required | {"aliases"}):
                    raise ValueError("Fact requires slot, value, source quote and span")
                span = fact["span"]
                if (
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(type(v) is not int for v in span)
                    or not 0 <= span[0] < span[1] <= len(text)
                    or not isinstance(fact["quote"], str)
                    or text[span[0] : span[1]] != fact["quote"]
                    or not isinstance(fact["value"], str)
                    or not fact["value"]
                    or fact["value"] not in fact["quote"]
                ):
                    raise ValueError("Fact source quote/span does not support its value")
                supported = next(
                    (f for f in extracted if f["slot"] == fact["slot"] and f["value"] == fact["value"]), None
                )
                if supported is None:
                    raise ValueError("Fact is not supported by the original sentence")
                if "aliases" in fact and fact["aliases"] != supported.get("aliases", []):
                    raise ValueError("Fact aliases must be supported by the original sentence")
                if supported not in selected:
                    selected.append(supported)
            extracted = selected
        existing = self.store.one(
            "SELECT id,text,kind FROM grounded_memory WHERE scope=? AND character_id=? "
            "AND character_version=? AND event_id=? AND role=?",
            (scope, character_id, version, event_id, role),
        )
        if existing:
            if existing["text"] != text or existing["kind"] != kind:
                raise ValueError("An existing source event cannot be rewritten")
            return existing["id"]
        cursor = self.store.execute(
            "INSERT INTO grounded_memory(scope,character_id,character_version,event_id,role,text,kind,fictional,facts,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                scope,
                character_id,
                version,
                event_id,
                role,
                text,
                kind,
                int(role == "assistant" or kind.startswith("role_")),
                json.dumps(extracted, ensure_ascii=False),
                time.time(),
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _identity(character):
        character_id, version = character.get("id"), character.get("version")
        if not isinstance(character_id, str) or not character_id or type(version) is not int or version < 1:
            raise ValueError("Memory requires an explicit character ID and version")
        return character_id, version

    def retrieve(self, scope, character, question, limit=3, max_chars=1800):
        character_id, version = self._identity(character)
        requested = required_slots(question)
        result = {"question": question, "required_slots": requested, "evidence": []}
        if limit <= 0 or max_chars <= 0:
            return result
        rows = self.store.all(
            "SELECT * FROM grounded_memory WHERE scope=? AND character_id=? AND character_version=? "
            "AND kind NOT IN ('derived_reply','grounded_reply') ORDER BY id DESC LIMIT 200",
            (scope, character_id, version),
        )
        sources = [
            (row, json.loads(row["facts"]))
            for row in rows
            if row["kind"] not in {"derived_reply", "grounded_reply"}
        ]
        retrospective = re.search(
            r"刚才|刚刚|上次|上回|之前|先前|那次|那天|那会|去了|去过|去哪里了|"
            r"送了|送给|给了|收到|拿到|得到|\bearlier\b|\bpreviously\b|"
            r"\bwhere did you go\b|\bwhat did\b.{0,40}\bgive\b",
            question,
            re.I,
        )
        source_mentioned = any(
            row["event_id"] in question
            or any(
                len(value) >= 2 and value.casefold() in question.casefold()
                for fact in facts
                if fact["slot"] in {"location", "person", "item"}
                for value in [fact["value"], *fact.get("aliases", [])]
            )
            for row, facts in sources
        )
        # Generic identities/places ("谁是…", "你家在哪里", "what's up")
        # are not requests to recite a past story. Lexical retrieval may still
        # supply a related original, without imposing its factual slots.
        if not retrospective and not source_mentioned:
            requested = []
            result["required_slots"] = []
        terms = _terms(question)
        scored = []
        for row, facts in sources:
            relevant = [f for f in facts if f["slot"] in requested]
            entity_matches = sum(
                any(v in question for v in [f["value"], *f.get("aliases", [])])
                for f in facts
                if f["slot"] != "action"
            )
            overlap = len(terms & _terms(row["text"]))
            # Matching occurred facts outrank a more recent plan or viewer query.
            score = (
                60 * (row["event_id"] in question)
                + entity_matches * 20
                + len({f["slot"] for f in relevant}) * 6
                + min(overlap, 5)
            )
            if not score or (not requested and not entity_matches and overlap < 2):
                continue
            scored.append((score, row["id"], row, relevant if requested else facts))
        # For factual follow-ups, bind slots to one corresponding source event.
        # Returning unrelated older stories could silently mix place/person/items.
        if requested and any(item[3] for item in scored):
            scored = [item for item in scored if item[3]]
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if requested and scored and scored[0][3]:
            target_event = scored[0][2]["event_id"]
            scored = [item for item in scored if item[2]["event_id"] == target_event]
        used = 0
        for _, _, row, facts in scored[: min(limit, 10)]:
            entry = {
                "source_id": row["id"],
                "event_id": row["event_id"],
                "role": row["role"],
                "kind": row["kind"],
                "fictional": bool(row["fictional"]),
                "text": row["text"],
                "facts": facts,
            }
            remaining = max_chars - used
            if len(json.dumps(entry, ensure_ascii=False)) > remaining:
                # A bounded original excerpt is still a source, not a summary.
                origin = facts[0]["span"][0] if facts else 0
                entry["text"] = row["text"][origin : origin + max(0, remaining // 2)]
                entry["source_start"] = origin
                entry["truncated"] = True
            size = len(json.dumps(entry, ensure_ascii=False))
            if size > remaining or not entry["text"]:
                continue
            result["evidence"].append(entry)
            used += size
        return result

    missing_slots = staticmethod(missing_slots)
