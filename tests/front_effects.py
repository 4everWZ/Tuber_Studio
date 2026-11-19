"""Select a registered frontend effect in model doubles."""

from tuber_studio.agent.workbench_tools import front_routes


def effect(action, group=None):
    return next(label for label, tool in front_routes().items() if tool == (action, group))
