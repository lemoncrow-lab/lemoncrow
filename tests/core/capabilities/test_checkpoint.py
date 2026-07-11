from __future__ import annotations

import time
from pathlib import Path

from lemoncrow.core.capabilities.owned_agent_session.checkpoint import (
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    cp = save_checkpoint("sess-1", messages, label="first", root=tmp_path)
    assert cp.message_count == 2
    assert cp.label == "first"
    assert Path(cp.snapshot_path).exists()

    restored = load_checkpoint(cp.id, "sess-1", root=tmp_path)
    assert restored == messages


def test_list_checkpoints_sorted(tmp_path: Path) -> None:
    cp1 = save_checkpoint("sess-1", [{"role": "user", "content": "a"}], root=tmp_path)
    time.sleep(0.01)
    cp2 = save_checkpoint("sess-1", [{"role": "user", "content": "b"}], root=tmp_path)
    # Different session should not appear.
    save_checkpoint("sess-2", [{"role": "user", "content": "c"}], root=tmp_path)

    cps = list_checkpoints("sess-1", root=tmp_path)
    assert [c.id for c in cps] == [cp2.id, cp1.id]


def test_load_missing_checkpoint_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        load_checkpoint("cp-nope", "sess-1", root=tmp_path)
