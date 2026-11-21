import { studioPath } from "./api.js";
import { mentionLinks } from "./public-mentions.js";
import { node, safeMessage } from "./core.js?v=20261105.32";

export function dynamicPublicationApproval(root, task, approval, api, onChange) {
  const heading = node("h2", "确认公开发布动态");
  const form = node("form");
  const fields = node("fieldset");
  fields.append(node("legend", approval.target));
  const carried = new Set(approval.carried_ids || []);
  const selected = new Set(carried);
  const drafts = approval.posts.filter(item => !carried.has(item.post_id));
  if (carried.size) fields.append(node("p", `${carried.size} 条内容未变，已保留确认。请核对有变化的草稿。`, "hint"));
  const confirm = node("button", "确认发布"), cancel = node("button", "取消");
  confirm.type = "submit";
  confirm.className = "primary";
  cancel.type = "button";
  const expiry = Number(approval.expires_at) * 1000;
  const note = node("p", "", "hint");
  note.setAttribute("role", "status");
  function refresh() {
    const expired = Date.now() >= expiry;
    confirm.disabled = expired || !selected.size;
    confirm.textContent = selected.size ? `确认发布 ${selected.size} 条` : "确认发布";
    if (expired) note.textContent = "确认已过期，请重新选择草稿。";
  }
  if (drafts.length > 1) {
    const all = node("button", "全选");
    all.type = "button";
    all.onclick = () => {
      for (const input of fields.querySelectorAll("input[type=checkbox]")) {
        input.checked = true;
        selected.add(Number(input.value));
      }
      refresh();
    };
    fields.append(all);
  }
  for (const item of drafts) {
    const caption = item.text.replace(/\s+/g, " ").slice(0, 72);
    const label = node("label"), input = node("input");
    input.type = "checkbox";
    input.value = item.post_id;
    input.onchange = () => {
      if (input.checked) selected.add(item.post_id);
      else selected.delete(item.post_id);
      refresh();
    };
    label.append(input, ` 草稿 #${item.post_id} · ${caption}`);
    const details = node("details");
    details.append(node("summary", item.image_hashes?.length ? "查看原文和配图" : "查看原文"),
                   node("blockquote", item.text, "quoted-text"));
    if (item.payload?.mentions?.length) details.append(mentionLinks(item.payload.mentions, node));
    const imagePath = `/api/agent/tasks/${task.id}/approval/${task.step_id}/dynamics/${item.post_id}/images`;
    for (const index of (item.image_hashes || []).keys()) {
      const image = node("img", null, "dynamic-comment-card");
      image.loading = "lazy";
      image.alt = `随草稿 #${item.post_id} 发布的配图 ${index + 1}`;
      image.src = studioPath(`${imagePath}/${index}`);
      details.append(image);
    }
    fields.append(label, details);
  }
  for (const item of approval.excluded || []) {
    fields.append(node("p", `草稿 #${item.post_id} 暂不可发布 · ${item.reason}`, "hint"));
  }
  const actions = node("div", null, "actions");
  if (approval.posts.length) actions.append(confirm);
  actions.append(cancel);
  form.append(fields, actions, note);
  root.append(heading, form);
  refresh();

  async function apply(action) {
    if (action === "approve" && (Date.now() >= expiry || !selected.size)) {
      refresh();
      return;
    }
    fields.disabled = confirm.disabled = cancel.disabled = true;
    try {
      const result = await api(`/api/agent/tasks/${task.id}`, {
        method: "PATCH", body: { action, step_id: task.step_id, revision: task.revision,
          ...(action === "approve" ? { selected_ids: [...selected] } : {}) },
      });
      onChange(result);
    } catch (error) {
      note.textContent = safeMessage(error.message);
      fields.disabled = cancel.disabled = false;
      refresh();
    }
  }
  form.onsubmit = event => { event.preventDefault(); void apply("approve"); };
  cancel.onclick = () => void apply("cancel");
}
