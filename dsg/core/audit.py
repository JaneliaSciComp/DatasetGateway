"""Central audit-logging helper.

All mutation paths use ``log_audit()`` instead of importing AuditLog
directly, so every entry is created through a single code-path.
"""

import logging

from core.models import AuditLog

logger = logging.getLogger(__name__)


def log_audit(actor, action, target_type, target_id, *, before_state=None, after_state=None):
    """Create an AuditLog entry.

    Parameters
    ----------
    actor : User | None
        The user who performed the action.
    action : str
        Short verb, e.g. ``"grant_created"``, ``"user_deactivated"``.
    target_type : str
        Model name, e.g. ``"Grant"``, ``"User"``, ``"Group"``.
    target_id : str | int
        Primary key (or other identifier) of the target object.
    before_state : dict | None
        JSON-serialisable snapshot *before* the mutation.
    after_state : dict | None
        JSON-serialisable snapshot *after* the mutation.
    """
    try:
        AuditLog.objects.create(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            before_state=before_state,
            after_state=after_state,
        )
    except Exception:
        logger.exception("Failed to write audit log entry: %s %s", action, target_id)
