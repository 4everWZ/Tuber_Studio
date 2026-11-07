const completedStatuses = new Set(["published", "rejected", "copied", "private_handoff"]);

export const commentFinished = comment => completedStatuses.has(comment?.draft?.status ?? comment?.status);
export const commentCanGenerate = comment => !comment.draft;

export function reconcileCommentSelection(model) {
  const available = new Set((model.comments || []).filter(commentCanGenerate).map(comment => comment.id));
  for (const id of model.selectedComments) if (!available.has(id)) model.selectedComments.delete(id);
}
