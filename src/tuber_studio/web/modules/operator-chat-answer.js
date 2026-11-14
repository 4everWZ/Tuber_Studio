import markdownit from "../vendor/markdown-it-15.0.3.mjs";
import { node } from "./core.js";

const markdown = markdownit({ html: false }).disable(["link", "image", "autolink"]);
const labels = { comment: "原评论", subtitle: "字幕", visual: "画面观察", reply: "回复记录" };
const replyLabels = {
  confirmed: "已送达的回复", uncertain: "送达待核对", owner_replied: "号主代回",
  previously_replied: "此前已回", same_account_replied: "同账号已回，归属未确认",
};
const tableRule = /^\s*\|(?:\s*:?[—–－-]+:?\s*\|){2,}\s*$/;

function normalizeTableRules(text) {
  let fence = "";
  return text.split("\n").map((line, index, lines) => {
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker) {
      if (!fence) fence = marker;
      else if (marker[0] === fence[0] && marker.length >= fence.length) fence = "";
      return line;
    }
    if (!fence && line.trimStart().startsWith("|") && lines[index - 1]?.trim()
        && !lines[index - 1].trimStart().startsWith("|")
        && tableRule.test(lines[index + 1] || "")) return "\n" + line;
    if (fence || !tableRule.test(line)) return line;
    return line.replace(/[—–－-]+/g, "---");
  }).join("\n");
}

function sourceLink(href) {
  const link = node("a", "打开原始来源");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function preview(group) {
  const content = node("div", null, "chat-source-preview");
  content.append(node("strong", group.title));
  for (const entry of group.entries) {
    const row = node("section");
    const time = entry.start == null ? "" : `P${entry.part} · ${Math.floor(entry.start / 60)}:${(entry.start % 60).toFixed(3).padStart(6, "0")}`;
    row.append(node("small", [replyLabels[entry.state] || labels[entry.kind], entry.author, entry.uid ? `UID ${entry.uid}` : "", time]
      .filter(Boolean).join(" · ")));
    if (entry.text) row.append(node("p", entry.text));
    if (entry.original_text) row.append(node("small", `B 站原字幕「${entry.original_text}」，已校正`));
    if (/^https:\/\/(?:www\.|t\.)bilibili\.com\//.test(entry.href || "")) row.append(sourceLink(entry.href));
    content.append(row);
  }
  return content;
}

function sourceChip(group, index) {
  const details = node("details", null, "chat-source-chip");
  const title = group.entries[0]?.kind === "comment" ? "原评论" : labels[group.entries[0]?.kind] || "来源";
  details.append(node("summary", title), preview(group));
  details.firstElementChild.setAttribute("aria-label", `来源 ${index + 1} · ${group.title}`);
  return details;
}

export function renderAssistantAnswer(root, turn) {
  const sources = turn.sources || [];
  root.replaceChildren();
  if (turn.reply_sections?.length) {
    const stories = node("ol", null, "chat-stories");
    turn.reply_sections.forEach((text, index) => {
      const item = node("li");
      item.append(node("p", text));
      if (sources[index]) item.append(sourceChip(sources[index], index));
      stories.append(item);
    });
    root.append(stories);
  } else if (turn.reply_format === "markdown") {
    root.innerHTML = markdown.render(normalizeTableRules(turn.reply_text || ""));
    for (const region of root.querySelectorAll("pre, table")) {
      region.tabIndex = 0;
      region.setAttribute("aria-label", region.tagName === "PRE" ? "代码" : "表格");
    }
  } else {
    root.append(node("p", turn.reply_text || ""));
  }
  if (!turn.reply_sections?.length && sources.length === 1) root.append(sourceChip(sources[0], 0));
  if (sources.length > 1) {
    const all = node("details", null, "chat-sources");
    all.append(node("summary", `来源 · ${sources.length}`), ...sources.map(preview));
    root.append(all);
  }
}
