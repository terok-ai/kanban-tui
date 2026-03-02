"""Tests for the luskctl kanban-tui backend with development workflow columns."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from kanban_tui.backends.luskctl.backend import (
    LuskctlBackend,
    _COLUMNS,
    _PHASE_PROMPTS,
    _PHASE_WORK_STATUS,
    _resolve_column,
)
from kanban_tui.config import LuskctlBackendSettings

_NOW = datetime.now()


def _setup_project(config_root: Path, state_root: Path, project_id: str, **proj_kw):
    """Create a minimal project config and return its root."""
    proj_dir = config_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"project": {"id": project_id, "security_class": "online", **proj_kw}}
    (proj_dir / "project.yml").write_text(yaml.safe_dump(cfg))
    return proj_dir


def _setup_task(state_root: Path, project_id: str, task_id: str, **meta_kw):
    """Create a task metadata YAML file."""
    meta_dir = state_root / "projects" / project_id / "tasks"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {"task_id": task_id, "name": f"task-{task_id}", **meta_kw}
    (meta_dir / f"{task_id}.yml").write_text(yaml.safe_dump(meta))


def _setup_work_status(state_root: Path, project_id: str, task_id: str,
                        status: str, message: str | None = None):
    """Create a work-status.yml in agent-config."""
    ac_dir = state_root / "projects" / project_id / "tasks" / task_id / "agent-config"
    ac_dir.mkdir(parents=True, exist_ok=True)
    data = {"status": status}
    if message:
        data["message"] = message
    (ac_dir / "work-status.yml").write_text(yaml.safe_dump(data))


def _setup_pending_phase(state_root: Path, project_id: str, task_id: str,
                          phase: str, prompt: str):
    """Create a pending-phase.yml in agent-config."""
    ac_dir = state_root / "projects" / project_id / "tasks" / task_id / "agent-config"
    ac_dir.mkdir(parents=True, exist_ok=True)
    (ac_dir / "pending-phase.yml").write_text(
        yaml.safe_dump({"phase": phase, "prompt": prompt})
    )


@pytest.fixture
def luskctl_env(tmp_path: Path):
    """Set up a luskctl-like filesystem environment."""
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    config_root.mkdir()
    state_root.mkdir()
    return config_root, state_root


@pytest.fixture
def single_project_backend(luskctl_env):
    """Backend with one project and three tasks."""
    config_root, state_root = luskctl_env

    _setup_project(config_root, state_root, "myproj")
    _setup_task(state_root, "myproj", "1", mode=None)  # created
    _setup_task(state_root, "myproj", "2", mode="cli", exit_code=0)  # completed
    _setup_task(state_root, "myproj", "3", mode="run", preset="solo")  # not found

    settings = LuskctlBackendSettings(
        state_root=str(state_root),
        config_root=str(config_root),
        active_project_id="myproj",
    )
    return LuskctlBackend(settings)


# ---------- Column resolution ----------


class TestColumnResolution:
    def test_created_goes_to_ready(self):
        assert _resolve_column("created", None, None) == 1

    def test_running_no_status_goes_to_coding(self):
        assert _resolve_column("running", None, "run") == 2

    def test_running_coding_goes_to_coding(self):
        assert _resolve_column("running", "coding", "run") == 2

    def test_running_planning_goes_to_coding(self):
        assert _resolve_column("running", "planning", "run") == 2

    def test_running_debugging_goes_to_coding(self):
        assert _resolve_column("running", "debugging", "run") == 2

    def test_running_testing_goes_to_testing(self):
        assert _resolve_column("running", "testing", "run") == 3

    def test_running_reviewing_goes_to_review(self):
        assert _resolve_column("running", "reviewing", "run") == 4

    def test_running_documenting_goes_to_review(self):
        assert _resolve_column("running", "documenting", "run") == 4

    def test_running_done_goes_to_done(self):
        assert _resolve_column("running", "done", "run") == 5

    def test_running_blocked_goes_to_stopped(self):
        assert _resolve_column("running", "blocked", "run") == 6

    def test_running_error_goes_to_stopped(self):
        assert _resolve_column("running", "error", "run") == 6

    def test_completed_goes_to_done(self):
        assert _resolve_column("completed", None, "run") == 5

    def test_stopped_goes_to_stopped(self):
        assert _resolve_column("stopped", None, "run") == 6

    def test_failed_goes_to_stopped(self):
        assert _resolve_column("failed", None, "run") == 6

    def test_not_found_goes_to_stopped(self):
        assert _resolve_column("not found", None, "run") == 6

    def test_deleting_goes_to_stopped(self):
        assert _resolve_column("deleting", None, "run") == 6

    def test_running_unknown_status_goes_to_coding(self):
        assert _resolve_column("running", "thinking-hard", "run") == 2


# ---------- Board management ----------


class TestBoardManagement:
    def test_get_boards(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "alpha")
        _setup_project(config_root, state_root, "beta")

        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)

        boards = backend.get_boards()
        assert len(boards) == 2
        assert boards[0].name == "alpha"
        assert boards[1].name == "beta"

    def test_gatekeeping_icon(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(
            config_root, state_root, "secure", security_class="gatekeeping"
        )

        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)
        boards = backend.get_boards()
        assert boards[0].icon == "\U0001f512"

    def test_active_board(self, single_project_backend):
        board = single_project_backend.active_board
        assert board.name == "myproj"

    def test_active_board_fallback_to_first(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "first")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="nonexistent",
        )
        backend = LuskctlBackend(settings)
        board = backend.active_board
        assert board.name == "first"

    def test_no_projects_raises(self, luskctl_env):
        config_root, state_root = luskctl_env
        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)
        with pytest.raises(Exception, match="No luskctl projects found"):
            _ = backend.active_board

    def test_board_infos(self, single_project_backend):
        infos = single_project_backend.get_board_infos()
        assert len(infos) == 1
        assert infos[0]["name"] == "myproj"
        assert infos[0]["amount_tasks"] == 3
        assert infos[0]["amount_columns"] == len(_COLUMNS)

    def test_board_markers(self, single_project_backend):
        board = single_project_backend.active_board
        assert board.reset_column == 1   # Ready
        assert board.start_column == 2   # Coding
        assert board.finish_column == 5  # Done


# ---------- Column management ----------


class TestColumnManagement:
    def test_get_columns_returns_six(self, single_project_backend):
        columns = single_project_backend.get_columns()
        assert len(columns) == 6
        names = [c.name for c in columns]
        assert names == ["Ready", "Coding", "Testing", "Review", "Done", "Stopped"]

    def test_get_column_by_id(self, single_project_backend):
        col = single_project_backend.get_column_by_id(3)
        assert col is not None
        assert col.name == "Testing"

    def test_get_column_by_id_nonexistent(self, single_project_backend):
        assert single_project_backend.get_column_by_id(99) is None


# ---------- Task management ----------


class TestTaskManagement:
    def test_get_tasks_on_active_board(self, single_project_backend):
        """Tasks are read from YAML; container states are empty (no podman)."""
        tasks = single_project_backend.get_tasks_on_active_board()
        assert len(tasks) == 3

    def test_created_task_in_ready_column(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task1 = next(t for t in tasks if t.task_id == 1)
        # mode=None, no container -> "created" -> column 1 (Ready)
        assert task1.column == 1

    def test_completed_task_in_done_column(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task2 = next(t for t in tasks if t.task_id == 2)
        # mode="cli", exit_code=0, no container -> "completed" -> column 5 (Done)
        assert task2.column == 5

    def test_not_found_task_in_stopped_column(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task3 = next(t for t in tasks if t.task_id == 3)
        # mode="run", no exit_code, no container -> "not found" -> column 6 (Stopped)
        assert task3.column == 6

    def test_task_metadata_includes_container_status(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task3 = next(t for t in tasks if t.task_id == 3)
        assert task3.metadata["project_id"] == "myproj"
        assert task3.metadata["mode"] == "run"
        assert task3.metadata["preset"] == "solo"
        assert task3.metadata["source"] == "luskctl"
        assert task3.metadata["container_status"] == "not found"

    def test_task_title_from_name(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task1 = next(t for t in tasks if t.task_id == 1)
        assert task1.title == "task-1"

    def test_get_task_by_id(self, single_project_backend):
        task = single_project_backend.get_task_by_id(2)
        assert task is not None
        assert task.task_id == 2

    def test_get_task_by_id_nonexistent(self, single_project_backend):
        assert single_project_backend.get_task_by_id(999) is None

    def test_get_tasks_by_ids(self, single_project_backend):
        tasks = single_project_backend.get_tasks_by_ids([1, 3])
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {1, 3}

    def test_empty_project_returns_no_tasks(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "empty")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="empty",
        )
        backend = LuskctlBackend(settings)
        assert backend.get_tasks_on_active_board() == []


# ---------- Work status in cards ----------


class TestWorkStatusInCards:
    def test_work_status_in_metadata(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")
        _setup_work_status(state_root, "proj", "1", "testing", "Running unit tests")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].metadata["work_status"] == "testing"


# ---------- Card descriptions ----------


class TestCardDescriptions:
    def test_stopped_task_shows_status(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run", exit_code=1)

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert "Failed (exit code 1)" in tasks[0].description

    def test_blocked_indicator(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")
        _setup_work_status(state_root, "proj", "1", "blocked", "Need API key")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert "Agent reports: blocked" in tasks[0].description
        assert "Need API key" in tasks[0].description

    def test_pending_phase_indicator(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")
        _setup_pending_phase(state_root, "proj", "1", "testing", "Run tests")
        # Since container is not running, pending phase gets auto-executed.
        # For this test, just check that the function works end-to-end.
        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        # Pending phase auto-execution will try task_followup (which will fail silently)
        with patch("kanban_tui.backends.luskctl.cli_bridge.task_followup", return_value=True):
            tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

    def test_mode_category_mapping(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="cli")
        _setup_task(state_root, "proj", "2", mode="web")
        _setup_task(state_root, "proj", "3", mode="run")
        _setup_task(state_root, "proj", "4", mode=None)

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()

        task_cats = {t.task_id: t.category for t in tasks}
        assert task_cats[1] == 1  # CLI
        assert task_cats[2] == 2  # Web
        assert task_cats[3] == 3  # Autopilot
        assert task_cats[4] is None  # No mode


# ---------- Deferred phase transitions ----------


class TestDeferredPhaseTransition:
    def test_running_autopilot_writes_pending_phase(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        # Simulate a card move to Testing (col 3) while running
        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,  # Testing
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)

        # Check pending-phase.yml was written
        from kanban_tui.backends.luskctl.data_reader import read_pending_phase

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        pp = read_pending_phase(ac_dir)
        assert pp is not None
        assert pp.phase == "testing"
        assert pp.prompt == _PHASE_PROMPTS[3]

    def test_running_autopilot_done_writes_pending_done(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=5,  # Done
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)

        from kanban_tui.backends.luskctl.data_reader import read_pending_phase

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        pp = read_pending_phase(ac_dir)
        assert pp is not None
        assert pp.phase == "done"


# ---------- Immediate phase transitions ----------


class TestImmediatePhaseTransition:
    @patch("kanban_tui.backends.luskctl.cli_bridge.task_followup", return_value=True)
    def test_stopped_autopilot_calls_followup(self, mock_followup, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,  # Testing
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)

        mock_followup.assert_called_once_with("proj", "1", _PHASE_PROMPTS[3])

        # Check work-status.yml was written
        from kanban_tui.backends.luskctl.data_reader import read_work_status

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        ws = read_work_status(ac_dir)
        assert ws.status == "testing"

    def test_stopped_autopilot_done_writes_status(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=5,  # Done
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)

        from kanban_tui.backends.luskctl.data_reader import read_work_status

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        ws = read_work_status(ac_dir)
        assert ws.status == "done"


# ---------- Auto-execution of pending phases ----------


class TestPendingPhaseAutoExecution:
    @patch("kanban_tui.backends.luskctl.cli_bridge.task_followup", return_value=True)
    def test_stopped_with_pending_triggers_followup(self, mock_followup, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")
        _setup_pending_phase(state_root, "proj", "1", "testing", "Run tests")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        # Getting tasks triggers auto-execution
        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

        mock_followup.assert_called_once_with("proj", "1", "Run tests")

        # pending-phase.yml should be cleared
        from kanban_tui.backends.luskctl.data_reader import read_pending_phase

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        assert read_pending_phase(ac_dir) is None

    def test_stopped_with_pending_done_writes_status(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")
        _setup_pending_phase(state_root, "proj", "1", "done", "")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

        from kanban_tui.backends.luskctl.data_reader import read_work_status

        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        ws = read_work_status(ac_dir)
        assert ws.status == "done"


# ---------- Interactive task blocking ----------


class TestInteractiveTaskBlocking:
    def test_running_interactive_all_moves_blocked(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="cli")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        # Try moving to Testing — should be blocked
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,
            metadata={
                "project_id": "proj",
                "mode": "cli",
                "container_status": "running",
            },
        )
        # Should not raise, just do nothing
        backend.update_task_status(task)

        # No pending phase or work status should be written
        ac_dir = state_root / "projects" / "proj" / "tasks" / "1" / "agent-config"
        assert not (ac_dir / "pending-phase.yml").exists()

    @patch("kanban_tui.backends.luskctl.cli_bridge.task_restart", return_value=True)
    def test_stopped_interactive_restart_to_coding(self, mock_restart, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="web")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=2,  # Coding
            metadata={
                "project_id": "proj",
                "mode": "web",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)
        mock_restart.assert_called_once_with("proj", "1")


# ---------- Unstarted task blocking ----------


class TestUnstartedTaskBlocking:
    def test_unstarted_all_moves_blocked(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode=None)

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        for target_col in (2, 3, 4, 5):
            task = Task(
                task_id=1,
                title="task-1",
                creation_date=_NOW,
                column=target_col,
                metadata={
                    "project_id": "proj",
                    "mode": None,
                    "container_status": "created",
                },
            )
            # Should not raise, just do nothing
            backend.update_task_status(task)


# ---------- Invalid move blocking ----------


class TestInvalidMoveBlocking:
    def test_move_to_ready_blocked(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=1,  # Ready
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)
        # No side effects — verified by no exception

    def test_move_to_stopped_blocked(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="run")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)

        from kanban_tui.classes.task import Task

        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=6,  # Stopped
            metadata={
                "project_id": "proj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)
        # No side effects — verified by no exception


# ---------- Category management ----------


class TestCategoryManagement:
    def test_get_all_categories(self, single_project_backend):
        cats = single_project_backend.get_all_categories()
        assert len(cats) == 3
        names = [c.name for c in cats]
        assert "CLI" in names
        assert "Web" in names
        assert "Autopilot" in names

    def test_get_category_by_id(self, single_project_backend):
        cat = single_project_backend.get_category_by_id(1)
        assert cat.name == "CLI"


# ---------- Not implemented operations ----------


class TestNotImplementedOperations:
    def test_create_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_new_board("Test")

    def test_delete_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.delete_board(1)

    def test_update_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.update_board(1, "name", "icon")

    def test_create_category_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_new_category("Test", "#fff")

    def test_update_column_visibility_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.update_column_visibility(1, False)

    def test_create_dependency_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_task_dependency(1, 2)

    def test_would_create_cycle_returns_false(self, single_project_backend):
        assert single_project_backend.would_create_dependency_cycle(1, 2) is False
