import { apiGet, apiPost } from "@/shared/api/client";

export interface ReplyHandoffItem {
  channel: "comments" | "messages";
  draft_id: number;
  source_ids: number[];
}

interface ReplySelectionScope {
  account: string;
  character: string;
  version: number;
  epoch: number;
}

interface PendingSelection {
  key: string;
  selectionId: string;
}

/**
 * Create and delegate an agent reply selection while caching the selection ID per scope+items
 * so retrying a failed delegate call does not create duplicate selection records.
 */
export function createReplyHandoff(): (items: ReplyHandoffItem[]) => Promise<{ task_id?: string }> {
  let pending: PendingSelection | undefined;

  return async (items: ReplyHandoffItem[]) => {
    const scope = await apiGet<"/api/v1/agent/reply-selections", ReplySelectionScope>(
      "/api/v1/agent/reply-selections"
    );
    const key = JSON.stringify([scope.account, scope.character, scope.version, scope.epoch, items]);
    if (pending?.key !== key) {
      const created = await apiPost<
        "/api/v1/agent/reply-selections",
        { selection_id: string },
        { epoch: number; items: ReplyHandoffItem[] }
      >("/api/v1/agent/reply-selections", {
        body: { epoch: scope.epoch, items },
      });
      pending = { key, selectionId: created.selection_id };
    }
    const result = await apiPost<
      "/api/v1/agent/reply-selections/{selection_id}/delegate",
      { task_id?: string },
      never
    >("/api/v1/agent/reply-selections/{selection_id}/delegate", {
      pathParams: { selection_id: pending.selectionId },
    });
    pending = undefined;
    return result;
  };
}
