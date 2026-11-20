"""Bind a direct action to a fixture's current owner message."""

def direct_request(text, *, mode="read", target=None, followup=False):
    return {"action": "direct_tool", "operation": {
        "request": text, "target": target or text, "mode": mode, "followup": followup,
    }}
