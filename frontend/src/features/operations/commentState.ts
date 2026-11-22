import type { CommentRecord } from "./types";

const COMPLETED_STATUSES = new Set(["published", "rejected", "copied", "private_handoff"]);

export function commentFinished(
  comment: { status?: string | null; draft?: { status?: string | null } | null } | null | undefined
): boolean {
  const status = comment?.draft?.status ?? comment?.status;
  return Boolean(status && COMPLETED_STATUSES.has(status));
}

export function commentCanGenerate(comment: Pick<CommentRecord, "draft"> | null | undefined): boolean {
  return !comment?.draft;
}

export function reconcileCommentSelection(
  comments: readonly CommentRecord[],
  selectedIds: ReadonlySet<number>
): Set<number> {
  const available = new Set(comments.filter(commentCanGenerate).map((comment) => comment.id));
  const next = new Set<number>();
  for (const id of selectedIds) {
    if (available.has(id)) {
      next.add(id);
    }
  }
  return next;
}
