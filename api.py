"""Compatibility facade for the feature-oriented application services.

New HTTP routes use ``api_service`` directly. The function aliases below keep
existing scripts and tests compatible while the domain module is migrated in
smaller, low-risk steps.
"""

from services.api_services import api_service
from services.application import (
    STATE,
    build_event_export,
    handle_event_delete,
    handle_event_save,
    handle_history_apply,
    handle_history_rollback,
    handle_roles,
    public_state,
    run_raffle,
)


handle_action = api_service.dispatch
handle_upload = api_service.raffle.upload
handle_history_upload = api_service.history.upload


__all__ = [
    "STATE",
    "api_service",
    "build_event_export",
    "handle_action",
    "handle_event_delete",
    "handle_event_save",
    "handle_history_apply",
    "handle_history_rollback",
    "handle_history_upload",
    "handle_roles",
    "handle_upload",
    "public_state",
    "run_raffle",
]
