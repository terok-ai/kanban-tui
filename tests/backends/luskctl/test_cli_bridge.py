"""Tests for the luskctl CLI bridge module."""

import subprocess
from unittest.mock import patch

from kanban_tui.backends.luskctl.cli_bridge import (
    luskctl_available,
    task_delete,
    task_new,
    task_restart,
    task_rename,
    task_stop,
)


class TestLuskctlAvailable:
    def test_returns_true_when_found(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/luskctl")
        assert luskctl_available() is True

    def test_returns_false_when_not_found(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert luskctl_available() is False


class TestTaskNew:
    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_parses_task_id(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Created task 3 (happy-hawk) in /path\n"
        )
        assert task_new("myproj") == "3"

    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_with_name(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Created task 5 (custom-name) in /path\n"
        )
        result = task_new("myproj", name="custom-name")
        assert result == "5"
        mock_run.assert_called_once_with("task", "new", "myproj", "--name", "custom-name")

    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_returns_none_on_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )
        assert task_new("myproj") is None

    def test_returns_none_when_binary_missing(self):
        with patch(
            "kanban_tui.backends.luskctl.cli_bridge._run_luskctl",
            side_effect=FileNotFoundError,
        ):
            assert task_new("myproj") is None


class TestTaskStop:
    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        assert task_stop("myproj", "1") is True

    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=""
        )
        assert task_stop("myproj", "1") is False


class TestTaskRestart:
    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        assert task_restart("myproj", "1") is True


class TestTaskDelete:
    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        assert task_delete("myproj", "1") is True


class TestTaskRename:
    @patch("kanban_tui.backends.luskctl.cli_bridge._run_luskctl")
    def test_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        assert task_rename("myproj", "1", "new-name") is True
        mock_run.assert_called_once_with("task", "rename", "myproj", "1", "new-name")
