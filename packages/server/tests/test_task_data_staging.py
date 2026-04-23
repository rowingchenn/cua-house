from __future__ import annotations

import pytest

from cua_house_common.events import JsonlEventLogger
from cua_house_common.models import TaskRequirement
from cua_house_server.data.staging import TaskDataManager


class RecordingTaskDataManager(TaskDataManager):
    def __init__(self, event_logger: JsonlEventLogger) -> None:
        super().__init__(task_data_root=None, event_logger=event_logger)
        self.commands: list[tuple[str, str]] = []

    def _container_exec(self, container_name: str, cmd: str) -> None:
        self.commands.append((container_name, cmd))

    async def _map_e_drive(self, client, cua_url: str) -> None:
        return None

    async def _mount_data_linux(self, client, cua_url: str) -> None:
        return None


def _task_data() -> TaskRequirement.TaskDataRequest:
    return TaskRequirement.TaskDataRequest(
        requires_task_data=True,
        source_relpath="bioinformatics/example/base",
        input_dir="E:\\agenthle\\bioinformatics\\example\\base\\input",
        software_dir="E:\\agenthle\\bioinformatics\\example\\base\\software",
        remote_output_dir="E:\\agenthle\\bioinformatics\\example\\base\\output",
    )


@pytest.mark.asyncio
async def test_symlink_staging_uses_lease_scoped_output_dir(tmp_path) -> None:
    manager = RecordingTaskDataManager(JsonlEventLogger(tmp_path / "events.jsonl", "test"))

    await manager.stage_phase(
        lease_id="lease-a",
        task_id="task-a",
        cua_url="http://127.0.0.1:5000",
        task_data=_task_data(),
        phase="runtime",
        container_name="container-a",
        use_symlink_inject=True,
        os_family="windows",
    )

    commands = [cmd for _, cmd in manager.commands]
    output_commands = [cmd for cmd in commands if "cua-house-lease-output" in cmd]
    assert len(output_commands) == 1
    assert "/storage/cua-house-lease-output/lease-a/bioinformatics/example/base/output" in output_commands[0]
    assert "/tmp/smb/agenthle/bioinformatics/example/base/output" in output_commands[0]
    assert "ln -sfn" in output_commands[0]


@pytest.mark.asyncio
async def test_same_task_two_leases_get_distinct_output_dirs(tmp_path) -> None:
    manager = RecordingTaskDataManager(JsonlEventLogger(tmp_path / "events.jsonl", "test"))

    for lease_id in ("lease-a", "lease-b"):
        await manager.stage_phase(
            lease_id=lease_id,
            task_id=f"task-{lease_id}",
            cua_url="http://127.0.0.1:5000",
            task_data=_task_data(),
            phase="runtime",
            container_name=f"container-{lease_id}",
            use_symlink_inject=True,
            os_family="windows",
        )

    output_commands = [cmd for _, cmd in manager.commands if "cua-house-lease-output" in cmd]
    assert len(output_commands) == 2
    assert "/storage/cua-house-lease-output/lease-a/bioinformatics/example/base/output" in output_commands[0]
    assert "/storage/cua-house-lease-output/lease-b/bioinformatics/example/base/output" in output_commands[1]
    assert output_commands[0] != output_commands[1]
