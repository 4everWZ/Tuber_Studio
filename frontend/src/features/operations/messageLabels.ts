export interface ContactLabelRow {
  direction?: string | null;
  contact_type?: string | null;
  type?: string | null;
  source_kind?: string | null;
  status?: string | null;
  outreach_status?: string | null;
  contact_state?: string | null;
}

const WAITING_STATE_LABELS: Record<string, string> = {
  awaiting_content: "等待符合联系目的的正文",
  awaiting_completion: "等待约定完成",
  awaiting_response: "等待对方回应",
  awaiting_invitation: "等待新的邀请或约定",
  check_pending: "首发联系核对中",
  reply_received: "已收到来信，转入普通回复",
};

const SPECIAL_STATUS_LABELS: Record<string, string> = {
  not_initiated: "未发起私信",
  generating: "生成中",
  cancelled: "已取消",
};

const CONTACT_REASON_LABELS: Record<string, string> = {
  long_followup: "楼内话题后续交流",
  private_details: "涉及私下交流内容",
  user_requested: "对方请求私下交流",
};

/** Display only recorded contact direction and states; unknown legacy direction stays unknown. */
export function contactLabel(row: ContactLabelRow | null | undefined): string {
  if (row?.direction === "incoming" || row?.contact_type === "incoming") return "收到私信";
  const type =
    row?.type ||
    row?.contact_type ||
    (row?.source_kind === "comment" ? "outreach" : row?.source_kind === "message" ? "reply" : null);
  return type === "outreach" ? "主动私信" : type === "reply" ? "回复来信" : "私信草稿";
}

export function contactStatus(row: ContactLabelRow | null | undefined): string | undefined {
  if (row?.status === "waiting") {
    const key = row.outreach_status || row.contact_state || "";
    return WAITING_STATE_LABELS[key] || "等待约定或对方回应";
  }
  return row?.status ? SPECIAL_STATUS_LABELS[row.status] : undefined;
}

export function contactReason(reason: string | null | undefined): string {
  if (!reason) return "";
  return CONTACT_REASON_LABELS[reason] || reason;
}
