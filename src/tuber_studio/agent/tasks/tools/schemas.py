"""Pydantic schemas and routing patterns for background task tools."""

import re
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from ....core.models import StrictModel
from ....live.facade import PvZAdapter, StardewValleyAdapter
from ..delivery import NON_NARRATIVE
from ..runtime import MAX_STEPS, ReceiptCondition

LOOKUP_TOOL = "workshop_lookup"
_CAPTION_TOPICS = (*NON_NARRATIVE, "故事内的人物、动作、对话或反应", "故事事件的起因、经过、结局或分析", "含义不清或混合")


class FarmAction(StrictModel):
    action: Literal["turn_right", "next_slot", "till_front", "plant_front", "water_front"]


class Search(StrictModel):
    query: str = Field(min_length=2, max_length=160)
    catalog: Literal["综合网页", "央视节目库"] | None = Field(default=None, description="按查证对象选公开检索库，节目用节目名查询。")
    news: bool = Field(default=False, strict=True, exclude_if=lambda value: not value,
                       description="本轮目标明确要求查近期新闻时启用。")
    bulletin: bool = Field(default=False, strict=True, exclude_if=lambda value: not value,
                           description="泛查新闻要闻、没有指定主题时读公开新闻快讯。")


class ResearchQuestion(StrictModel):
    search: Search | None = Field(description="仍能按现有资料自主查证时填一项具体检索；确实仅能由号主补充时填 null。")


class SourceWork(StrictModel):
    title: str = Field(description="取自已打开投稿标题的原作名。")
    kind: Literal["影视节目", "其它作品"]

class WorkClaim(StrictModel):
    phrase: str = Field(min_length=1, description="逐字引用含作品名或将镜头、人物归到特定作品或集的完整短语。")
    target: str = Field(min_length=1, description="短语内逐字出现的作品名、集号或指向特定作品的指代。")
    relation: Literal["提及节目资料", "转述画面可读题名", "断言镜头或人物的作品或集归属", "未能核实所述关系"]
    work_type: str = Field(default="", description="仅逐字摘取字段对这部作品明确所称的类型，无类型填空串。")

class Video(StrictModel):
    bvid: str = Field(pattern=r"^BV[0-9A-Za-z]{10}$")
    part: int | None = Field(default=None, ge=1, strict=True)
    start: float | None = Field(default=None, ge=0, strict=True)
    end: float | None = Field(default=None, gt=0, strict=True, description="区间右端（不包含）；须严格晚于 start。")

    @field_validator("end")
    @classmethod
    def end_after_start(cls, end, info: ValidationInfo):
        start = info.data.get("start")
        if end is not None and start is not None and end <= start:
            raise ValueError("结束秒须晚于开始秒，单帧时点也须落在左闭右开区间内")
        return end

    @model_validator(mode="after")
    def interval(self):
        if self.start is not None or self.end is not None:
            if self.part is None or self.start is None or self.end is None:
                raise ValueError("时段须指定分P、开始秒和更晚的结束秒")
        return self


class TextBrief(StrictModel):
    objective: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(min_length=1)


class EditorialIssue(StrictModel):
    field: str
    quote: str = Field(min_length=1)
    reason: Literal["引用未支持该断言", "单帧外推连续画面", "画面不能判断实际声音",
                    "网页出处外推画面身份或集号", "披露剧情", "选段画面披露剧情", "未经核实的运营效果",
                    "与可见屏字矛盾", "与取材覆盖回执矛盾", "与改口指定保留的原文矛盾"]


class VideoGroundingReview(StrictModel):
    supported: bool = Field(strict=True)
    issues: list[EditorialIssue] = Field(default_factory=list)


class RetainedVideoRevision(StrictModel):
    ordered_actions: list[Literal["保留整项原文", "替换为未用素材", "另读新窗口再替换", "其它编辑"]] = Field(description="严格按上一版顺序，对每一项各给一个操作。")


class ClipDecision(StrictModel):
    decision: Literal["采用", "另取材料", "材料不足"]
    primary_frame: str | None
    rationale: str = Field(min_length=1)


class ClipCaption(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=1200)


class VisibleFrame(StrictModel):
    labels: list[str] = Field(min_length=1, max_length=3, description="主帧可直接指认的构图短标题。")
    details: list[str] = Field(min_length=1, max_length=6, description="人物位置、服饰、物件、构图等可见细节。")
    screen_text: list[str] = Field(description="确实可读的屏字或水印原文。")


class ClipTitle(StrictModel):
    title: str = Field(min_length=1, max_length=300)

class EditingUses(StrictModel):
    opening: str = Field(min_length=1, description="开场时如何安排这一镜和第一句介绍的先后。")
    main: str = Field(min_length=1, description="在讲述中间哪个时机切入这一镜，何时切回来。")
    transition: str = Field(min_length=1, description="如何用这一镜连接相邻的两段讲述。")

class ClipBrief(StrictModel):
    requested_count: int | None = Field(description="号主明确要求的独立素材数量；没有指定则填 null。", ge=1)
    usage: str = Field(description="号主要求的选材用途和取舍标准。")
    playable: bool = Field(description="目标要交付可使用的片段或短片时为 true；仅问画面或文字资料为 false。")
    trace_origin: bool = Field(description="是否要求另查原出处或独立节目资料。")
    clip_seconds: float | None = Field(default=None, gt=0, description="号主明确指定的单个窗口秒数，未指定填 null。")


class WindowOrder(StrictModel):
    windows: list[str] = Field(description="按最新改口的优先顺序选出可继续核对的实际窗口 ID。")


class WindowProbe(StrictModel):
    read: str
    start: float = Field(ge=0)


class DiscoveryAssessment(StrictModel):
    material: str = Field(min_length=1, description="结合整帧描述和屏字判断素材片类，用号主任务相关的简短描述。")
    priority: Literal["优先补读", "备选", "不适合"]

class LiveText(StrictModel):
    text: str = Field(min_length=1, max_length=120)


class GameAction(StrictModel):
    game: Literal["stardew_valley", "pvz"]
    action: str = Field(min_length=1, max_length=40)


class ToolSequence(StrictModel):
    steps: list[str] = Field(min_length=1, max_length=MAX_STEPS)


class PlanningQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=1000)


_HTTPS_LINK = re.compile(r"https://[^\s<>\"'，。！？、；）]+")
_OWNER_LOOKUP = re.compile(r"bilibili_owner|号主|主人|绑定(?:的)?(?:账号|主页)|(?:你的|角色的)\s*(?:[Bb]\s*站|主页|空间)")
_OPERATIONS_LOOKUP = re.compile(r"operation_status|today_activity|运营|刷视频|自动评论|自动发布|私信|动态|趣事|看了")
_LIVE_CONTEXT = re.compile(r"live_(?:status|handoff)|直播|节目|推流|播控")
_VIDEO_ID = re.compile(r"BV[0-9A-Za-z]{10}")
_WEB_CONTEXT = re.compile(r"web_(?:search|page)|网页|网站|链接|检索|搜索|公告|资料")
_GAME_CONTEXT = re.compile(r"game_(?:status|action)|模拟游戏|星露谷|植物大战僵尸|农田|地块")
_GAME_PROPOSAL = re.compile(r"(?:准备|提议|安排|拟一)[^，。！？；]{0,32}(?:确认|审批)")
_PLAN_LIMIT = re.compile(r"(?:最多|总共只|只|限)\s*(?:读|做|查|执行)?\s*([1-8一二两三四五六七八])\s*(?:步|次|个来源)")
_NUMERIC_LIMITS = dict(zip("一二两三四五六七八", (1, 2, 2, 3, 4, 5, 6, 7, 8), strict=True))
_NO_RETRY = re.compile(r"不(?:要|得|再)?重试|不再尝试")
_REREAD = re.compile(r"反复|重复|重读|重试|重开|重新|再次|再(?:读|打开|查阅|尝试)|多次|[2-9二两三四五六七八九]\s*(?:遍|次)")
_EXPLICIT_VIDEO_READ = re.compile(
    r"(?:^|[。；，]\s*)(?:请\s*)?(?:先\s*|只读\s*)(?:取材|查看|核对|读取?|看)\s*(?:视频|BV)"
)
_CONDITIONAL_SEARCH = re.compile(
    r"^\s*(?:[,，]?\s*(?:没有画面|无画面))?\s*[,，]?\s*(?:就|则|便)?\s*(?:先|请)?\s*(?:搜索|检索|查找|web_search)"
)
_READ_AFTER_SEARCH = re.compile(r"(?:再|然后|随后|接着)\s*(?:请)?\s*(?:打开|读取?|查看|web_page)")
_CONDITION_CUES = {
    "video_has_only_subtitles": re.compile(r"(?:如果|若|只在|只有在|当)[^，。！？；]{0,25}(?:只有字幕|仅有字幕)"),
    "game_has_unwatered_crops": re.compile(r"(?:如果|若|只在|还有|仍有)[^，。！？；]{0,20}(?:没浇水|未浇水|待浇水|需要浇水)"),
}
_GAME_ACTIONS = {adapter.name: sorted(adapter.actions | {"step"}) for adapter in (PvZAdapter, StardewValleyAdapter)}
_RESULT_CONDITIONS = {
    "video_has_only_subtitles": ReceiptCondition(tool="inspect_video", path="viewing", operator="eq", value="subtitles_only"),
    "game_has_unwatered_crops": ReceiptCondition(tool="game_status", path="state.unwatered_crops", operator="gt", value=0),
}
