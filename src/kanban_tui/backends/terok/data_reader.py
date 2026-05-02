"""Re-export facade over terok library types for the kanban-tui backend.

All task/project/status logic lives in the terok Python package.
This module provides guarded imports so the rest of the backend can
use a single ``HAS_TEROK`` flag to check availability.
"""

from __future__ import annotations

from pathlib import Path

try:
    from terok.lib.containers.task_display import STATUS_DISPLAY  # noqa: F401
    from terok.lib.containers.task_runners import (  # noqa: F401
        task_followup_headless,
        task_restart,
    )
    from terok.lib.containers.tasks import (  # noqa: F401
        TaskMeta,
        get_all_task_states,
        get_tasks,
        task_delete,
        task_new,
        task_rename,
        task_stop,
    )
    from terok.lib.containers.work_status import (  # noqa: F401
        WORK_STATUS_DISPLAY,
        PendingPhase,
        WorkStatus,
        clear_pending_phase,
        read_pending_phase,
        read_work_status,
        write_pending_phase,
        write_work_status,
    )
    from terok.lib.core.projects import Project, list_projects, load_project  # noqa: F401

    HAS_TEROK = True
except ImportError:
    HAS_TEROK = False


def agent_config_dir(project: "Project", task_id: str) -> Path:
    """Return the agent-config directory for a task."""
    return project.tasks_root / task_id / "agent-config"
