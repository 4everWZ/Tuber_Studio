export function mentionLinks(mentions, node) {
  const list = node("div", null, "actions");
  for (const mention of mentions || []) {
    if (!/^[1-9][0-9]{0,19}$/.test(String(mention.uid))) continue;
    const link = node("a", `@${mention.name}`);
    link.href = `https://space.bilibili.com/${mention.uid}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = `B 站 UID ${mention.uid}`;
    list.append(link);
  }
  return list;
}

export function mentionEditor({ mentions, pending, editable, node, save }) {
  const section = node("section");
  if (mentions?.length) section.append(mentionLinks(mentions, node));
  if (pending?.length) section.append(node("p", `待选账号 ${pending.map(item => item.label).join("、")}`, "hint"));
  if (!editable) return section;

  const details = node("details"), fields = node("div");
  details.append(node("summary", "编辑 @"), fields);
  function addField(label = "", account = "", candidates = []) {
    const row = node("div", null, "actions");
    const target = node("input"), recipient = node("input");
    target.setAttribute("aria-label", "正文中的 @昵称");
    recipient.setAttribute("aria-label", "B 站 UID 或昵称");
    target.placeholder = "正文中的 @昵称";
    recipient.placeholder = "B 站 UID 或昵称";
    target.value = label;
    recipient.value = account;
    const remove = node("button", "移除");
    remove.type = "button";
    remove.onclick = () => row.remove();
    row.append(target, recipient, remove);
    for (const candidate of candidates) {
      const choose = node("button", `${candidate.name} · UID ${candidate.uid}`);
      choose.type = "button";
      choose.onclick = () => {
        target.value = `@${candidate.name}`;
        recipient.value = candidate.uid;
      };
      row.append(choose);
    }
    fields.append(row);
  }
  for (const item of mentions || []) addField(`@${item.name}`, String(item.uid));
  for (const item of pending || []) addField(item.label, item.account, item.candidates);
  const add = node("button", "添加 @"), apply = node("button", "保存提及");
  add.type = apply.type = "button";
  add.onclick = () => addField();
  apply.onclick = () => save([...fields.children].map(row => {
    const [label, account] = row.querySelectorAll("input");
    return { label: label.value.trim(), account: account.value.trim() };
  }).filter(item => item.label || item.account));
  details.append(add, apply);
  section.append(details);
  return section;
}
