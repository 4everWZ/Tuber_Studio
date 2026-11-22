"""L4 FastAPI route registration modules grouped by domain boundary."""

from .agent import register_agent_routes
from .agent_tasks import register_task_routes
from .entries import register_entry_routes
from .live import register_live_routes
from .operations import register_browsing_routes, register_operations_routes
from .operations_channels import register_operations_channel_routes
from .persona import register_persona_routes
from .system import register_system_routes

__all__ = [
    "register_agent_routes",
    "register_browsing_routes",
    "register_entry_routes",
    "register_live_routes",
    "register_operations_channel_routes",
    "register_operations_routes",
    "register_persona_routes",
    "register_system_routes",
    "register_task_routes",
]
