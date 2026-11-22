"""Discovery-side send admission (non-direct quota yielding) and claim coordination."""

from contextlib import contextmanager


@contextmanager
def discovery_comment_slot(browsing, row, kind, bvid=None, recipient=""):
    """Acquire a Discovery preparation slot (`direct=False`) respecting `direct_quota_reserve`."""
    with browsing.comment_admission.slot(row, kind, bvid, recipient, direct=False) as blocked:
        yield blocked


def claims_manager(browsing):
    """Return `OperationClaims` bound to `browsing.operations` or `browsing.store`."""
    ops = getattr(browsing, "operations", None)
    if ops is not None and getattr(ops, "claims", None) is not None:
        return ops.claims
    return None


def _has_inbox_priority_for_resource(claims, account_id, character_id, resource_type, resource_id, root_id, oid=None):
    for rid in dict.fromkeys(str(v) for v in (resource_id, oid) if v not in (None, "")):
        if claims.has_inbox_priority(
            account_id=account_id,
            character_id=character_id,
            resource_type=resource_type,
            resource_id=rid,
            root_id=str(root_id),
        ):
            return True
    return False


def try_claim_discovery_target(browsing, row, *, resource_type, resource_id, root_id, comment_id, oid=None):
    """Try acquiring Discovery floor lease + comment claim; yield immediately if Inbox owns/preempted it."""
    claims = claims_manager(browsing)
    if claims is None:
        return None
    account_id = str(row["account_id"])
    character_id = str(row["character_id"])
    if _has_inbox_priority_for_resource(
        claims, account_id, character_id, resource_type, resource_id, str(root_id), oid=oid
    ):
        return False
    floor = claims.acquire_floor_lease(
        account_id=account_id,
        character_id=character_id,
        resource_type=resource_type,
        resource_id=resource_id,
        root_id=str(root_id),
        owner_pipeline="discovery",
        ttl_seconds=180.0,
    )
    if not floor["acquired"]:
        return False
    comment = claims.claim_comment(
        account_id=account_id,
        character_id=character_id,
        resource_type=resource_type,
        resource_id=resource_id,
        root_id=str(root_id),
        target_comment_id=str(comment_id),
        owner_pipeline="discovery",
        ttl_seconds=120.0,
    )
    if not comment["acquired"]:
        claims.release(floor["claim_key"], lease_token=floor["lease_token"])
        return False
    return (claims, floor, comment)


def verify_discovery_claims(handle):
    """Return True if Discovery still holds both its floor lease and comment claim."""
    if handle is None:
        return True
    claims, floor, comment = handle
    return claims.verify_lease(floor["claim_key"], floor["lease_token"]) and claims.verify_lease(
        comment["claim_key"], comment["lease_token"]
    )


def release_discovery_claims(handle):
    """Release held Discovery floor/comment claims if still active."""
    if not handle:
        return
    claims, floor, comment = handle
    claims.release(comment["claim_key"], lease_token=comment["lease_token"])
    claims.release(floor["claim_key"], lease_token=floor["lease_token"])
