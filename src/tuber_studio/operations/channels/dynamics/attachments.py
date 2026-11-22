"""Comment card rendering, source link extraction, and dynamic image queuing."""


from ....core.models import content_hash
from ..attachments import checked_image, image_scope, queue_image
from .comment_card import comment_card, render_comment_card
from .materials import source_links


class DynamicsAttachmentsMixin:
    def _comment_card(self, sources, actor):
        browsing = getattr(self.runtime, "browsing", None)
        timezone = browsing.policy().get("business_timezone", "Asia/Taipei") if browsing else "Asia/Taipei"
        return comment_card(sources, store=self.store, actor=actor, timezone=timezone)

    def comment_card_image(self, post_id, page=0):
        card = self._post(post_id)["comment_card"]
        if not card:
            raise ValueError("这条动态没有原评论图")
        return render_comment_card(card, page)

    @staticmethod
    def _comment_links(row):
        selected = (row.get("presentation") or {}).get("link_source_ids")
        if selected is not None:
            by_id = {source["id"]: source for source in row["sources"]}
            return list(dict.fromkeys(link for identity in selected if identity in by_id
                                      for link in source_links(by_id[identity])))
        if card := row.get("source_comment_card") or row["comment_card"]:
            links = card["comments"] if card.get("legacy") else card["links"]
            return list(dict.fromkeys(item["url"] for item in links))
        return []

    def _publication_images(self, row):
        if card := row["comment_card"]:
            return [{"mime_type": "image/png", "content": render_comment_card(card, page)}
                    for page in range(card.get("page_count", 1))]
        if row.get("illustration"):
            return [checked_image(self.memes, row["illustration"], row["character_id"],
                                  runtime=self.runtime, scope=image_scope("dynamics", row))]
        if row.get("meme_id"):
            if not self.memes:
                raise ValueError("配图服务未连接，草稿已保存")
            return [self.memes.read(row["meme_id"], row["character_id"])]
        return []

    def _queue_image(self, post, candidate):
        public = [{key: source[key] for key in ("id", "type", "text", "source_url") if key in source}
                  for source in post["sources"]]
        return queue_image(self.runtime, "dynamics", post, public, post["content"],
                           comment_card=content_hash(candidate) if candidate else None, require_choice=True)
