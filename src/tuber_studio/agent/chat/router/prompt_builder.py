"""Prompt assembly helpers for the background turn router."""

import logging

from ....core.store import dump
from ..lookup import workbench_tools

logger = logging.getLogger("uvicorn.error").getChild("turn_router")

def _build_router_messages(char, data, packet, *, allow_identity_read=True):
    fields = workbench_tools.settings_catalog()
    setting_keys = {group: list(fields[group]["fields"]) for group in workbench_tools.settings_menu()}
    system_prompt = (
        f"你是{char['name']}的对话与能力路由器。结合最近对话与候选对象清单，每轮仅调用一个最准确的路由工具。\n"
        f"可调设置字段键：{dump(setting_keys)}\n"
        "核心边界（严格按优先级判断）：\n"
        "1. 闲聊、问候、玩笑、设想（假如/要是/如果）、否定/阻止操作（先别/别动/先别接新活），或只问当前几点/几号/星期几 -> chat（仅问当前时间/日期/星期时填 clock_fields）；但若明确要求记住/修改/忘掉长期偏好或事实时选 direct_action（save_memory/update_memory/delete_memory）。\n"
        "2. 凡是要求留下便笺/笔记/交接记录/总结/报告、「等我回来再看」、交给后台处理、同步图库/抓取二创图、制片或定时预约 -> 必须选 delegate_task（即使同时提到核实演播间/直播/运营状态，也不能选 instant_read），并按工作类型填 profile："
        "同步图库订阅源/抓取二创图/批量视觉筛选选 profile=meme_harvest；制作视频/Remotion渲染/投稿选 profile=video_production；视频分P抽帧核查选 profile=media_inspection；一般公开检索与状态调研选 profile=general_research。\n"
        "3. 对已有 task_id 改口、换重点、补充要求或取消 -> steer_task，且 change_quote 必须逐字复制对方原话里的新要求子句；与已有任务无关的日常闲聊必须选 chat。\n"
        "4. 准备发动态/评论回复/私信送审 -> 必须选 prepare_publication（操作待发队列，不受前文视频/观看记录影响）。\n"
        "5. 仅当场口头只读查询、不留便笺/报告 -> instant_read："
        "查今天/昨天/具体日期或已知BV的视频/原链接/已发动态用 workshop_lookup（必填 collection，day仅限today/yesterday/YYYY-MM-DD）；查数量用 watch_totals；"
        "按话题回查上周/过往原评、旧事或经历必须用 recall_memory（填 topic；追问 recalled_source 填 object_ref）；"
        "查当前设置必须填 action=daily_settings + settings_group + settings_read_only=true；问能调什么用 workbench_capabilities；"
        "问「待审箱有几张图」「订阅源上次同步状态」等只读图库状态（不触发同步抓取）必须用 meme_gallery_status"
        + (
            "；去看/联网核验绑定主人的 B 站主页用 owner_profile，查本地记录的绑定主页链接/UID 用 owner_binding，查运营登录账号用 operating_account，同时查用 identity_read。"
            if allow_identity_read
            else "。"
        )
        + "\n6. 仅明确要立即开启自主运营（start_operations）、修改设置、保存/更新/忘记记忆（save_memory/update_memory/delete_memory）、换宠物表情（pet_settings）、排完整看视频或停止（stop）时才选 direct_action。\n"
        "7. 当 candidates 中存在多个同类型对象（如多条视频）且只说“那条/那个”未给出序号/标题/BV 时 -> clarify；若候选分别对应评论与回复且对方明确说“那条评论”，直接按对应 object_ref 调 recall_memory。\n"
        "8. 只针对最后一条 user JSON 中的 message 字段决策；历史对话与 clarification 仅为可选背景。若对方换了新话题（如问当前时间/日期/星期、闲聊、或问别的事），必须完全忽略 clarification，严禁填 clarification_id，也严禁重复上轮澄清问题；仅当 message 明确在回答 clarification.question 时才填 clarification_id。"
    )
    recent = [
        {"role": item["role"], "content": item["content"]}
        for item in (packet.get("recent_dialogue") or ())
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    rest = {key: value for key, value in packet.items() if key != "recent_dialogue"}
    user_payload = {
        **rest,
        "message": data.message,
    }
    return [
        {"role": "system", "content": system_prompt},
        *recent,
        {"role": "user", "content": dump(user_payload)},
    ]
