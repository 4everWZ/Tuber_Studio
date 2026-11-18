const path = "/api/agent/reply-selections";

export function replyHandoff(api) {
  let pending;
  return async items => {
    const scope = await api(path);
    const key = JSON.stringify([scope.account, scope.character, scope.version, scope.epoch, items]);
    if (pending?.key !== key) {
      const { selection_id } = await api(path, { method: "POST", body: { epoch: scope.epoch, items } });
      pending = { key, selection_id };
    }
    const result = await api(`${path}/${pending.selection_id}/delegate`, { method: "POST" });
    pending = undefined;
    return result;
  };
}
