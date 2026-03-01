"""Tests for the luskctl data reader module."""

from pathlib import Path

import pytest
import yaml

from kanban_tui.backends.luskctl.data_reader import (
    LuskctlProjectInfo,
    LuskctlTaskMeta,
    discover_projects,
    effective_status,
    query_container_states,
    read_task_metas,
    resolve_task_container_state,
)


# ---------- effective_status ----------


class TestEffectiveStatus:
    def test_deleting_always_wins(self):
        assert effective_status("running", "cli", 0, deleting=True) == "deleting"

    def test_running(self):
        assert effective_status("running", "cli", None, False) == "running"

    def test_exited_exit_code_0(self):
        assert effective_status("exited", "cli", 0, False) == "completed"

    def test_exited_exit_code_nonzero(self):
        assert effective_status("exited", "cli", 1, False) == "failed"

    def test_exited_no_exit_code(self):
        assert effective_status("exited", "cli", None, False) == "stopped"

    def test_no_container_no_mode(self):
        assert effective_status(None, None, None, False) == "created"

    def test_no_container_with_mode_exit_0(self):
        assert effective_status(None, "run", 0, False) == "completed"

    def test_no_container_with_mode_exit_1(self):
        assert effective_status(None, "run", 1, False) == "failed"

    def test_no_container_with_mode_no_exit(self):
        assert effective_status(None, "cli", None, False) == "not found"


# ---------- discover_projects ----------


class TestDiscoverProjects:
    def test_discovers_projects(self, tmp_path: Path):
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        (proj_a / "project.yml").write_text(
            yaml.safe_dump({"project": {"id": "alpha", "security_class": "online"}})
        )

        proj_b = tmp_path / "beta"
        proj_b.mkdir()
        (proj_b / "project.yml").write_text(
            yaml.safe_dump({"project": {"id": "beta", "security_class": "gatekeeping"}})
        )

        projects = discover_projects(config_roots=[tmp_path])
        assert len(projects) == 2
        assert projects[0].project_id == "alpha"
        assert projects[0].security_class == "online"
        assert projects[1].project_id == "beta"
        assert projects[1].security_class == "gatekeeping"

    def test_skips_dirs_without_project_yml(self, tmp_path: Path):
        (tmp_path / "noproject").mkdir()
        projects = discover_projects(config_roots=[tmp_path])
        assert len(projects) == 0

    def test_empty_directory(self, tmp_path: Path):
        projects = discover_projects(config_roots=[tmp_path])
        assert len(projects) == 0

    def test_invalid_yaml_skipped(self, tmp_path: Path):
        proj = tmp_path / "broken"
        proj.mkdir()
        (proj / "project.yml").write_text("{{invalid yaml")
        projects = discover_projects(config_roots=[tmp_path])
        assert len(projects) == 0

    def test_user_overrides_system(self, tmp_path: Path):
        user_root = tmp_path / "user"
        sys_root = tmp_path / "system"
        user_root.mkdir()
        sys_root.mkdir()

        # System project
        sys_proj = sys_root / "myproj"
        sys_proj.mkdir()
        (sys_proj / "project.yml").write_text(
            yaml.safe_dump({"project": {"id": "myproj", "security_class": "online"}})
        )

        # User project with same ID
        user_proj = user_root / "myproj"
        user_proj.mkdir()
        (user_proj / "project.yml").write_text(
            yaml.safe_dump(
                {"project": {"id": "myproj", "security_class": "gatekeeping"}}
            )
        )

        # User root comes first => gets overridden by later (system) root
        # But user overrides system in real config
        projects = discover_projects(config_roots=[sys_root, user_root])
        assert len(projects) == 1
        # Last root wins
        assert projects[0].security_class == "gatekeeping"


# ---------- read_task_metas ----------


class TestReadTaskMetas:
    def _create_task_yaml(
        self, state_root: Path, project_id: str, task_id: str, **kwargs
    ):
        meta_dir = state_root / "projects" / project_id / "tasks"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta = {"task_id": task_id, "name": f"task-{task_id}", **kwargs}
        (meta_dir / f"{task_id}.yml").write_text(yaml.safe_dump(meta))

    def test_reads_tasks(self, tmp_path: Path):
        self._create_task_yaml(
            tmp_path, "proj1", "1", mode="cli", exit_code=None, name="happy-hawk"
        )
        self._create_task_yaml(
            tmp_path, "proj1", "2", mode="run", exit_code=0, preset="solo"
        )

        tasks = read_task_metas("proj1", state_root=tmp_path)
        assert len(tasks) == 2
        assert tasks[0].task_id == "1"
        assert tasks[0].mode == "cli"
        assert tasks[0].name == "happy-hawk"
        assert tasks[1].task_id == "2"
        assert tasks[1].exit_code == 0
        assert tasks[1].preset == "solo"

    def test_sorted_numerically(self, tmp_path: Path):
        for tid in ["10", "2", "1", "3"]:
            self._create_task_yaml(tmp_path, "proj1", tid)
        tasks = read_task_metas("proj1", state_root=tmp_path)
        assert [t.task_id for t in tasks] == ["1", "2", "3", "10"]

    def test_empty_project(self, tmp_path: Path):
        tasks = read_task_metas("nonexistent", state_root=tmp_path)
        assert tasks == []

    def test_invalid_yaml_skipped(self, tmp_path: Path):
        meta_dir = tmp_path / "projects" / "proj1" / "tasks"
        meta_dir.mkdir(parents=True)
        (meta_dir / "1.yml").write_text("{{broken yaml")
        self._create_task_yaml(tmp_path, "proj1", "2")

        tasks = read_task_metas("proj1", state_root=tmp_path)
        assert len(tasks) == 1
        assert tasks[0].task_id == "2"


# ---------- resolve_task_container_state ----------


class TestResolveTaskContainerState:
    def test_finds_running_container(self):
        meta = LuskctlTaskMeta(task_id="1", mode="cli")
        states = {"proj-cli-1": "running"}
        assert resolve_task_container_state("proj", meta, states) == "running"

    def test_no_mode_returns_none(self):
        meta = LuskctlTaskMeta(task_id="1", mode=None)
        states = {"proj-cli-1": "running"}
        assert resolve_task_container_state("proj", meta, states) is None

    def test_container_not_found(self):
        meta = LuskctlTaskMeta(task_id="2", mode="web")
        states = {"proj-cli-1": "running"}
        assert resolve_task_container_state("proj", meta, states) is None


# ---------- query_container_states ----------


class TestQueryContainerStates:
    def test_returns_empty_when_podman_unavailable(self, monkeypatch):
        """When podman is not installed, returns empty dict."""
        monkeypatch.setenv("PATH", "")
        result = query_container_states("testproject")
        assert result == {}
