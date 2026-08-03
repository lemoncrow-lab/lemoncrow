from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.cli.commands.eval import _EXTERNAL_CHANNELS, _channel_cmd_env


def test_graft_is_a_first_class_external_retrieval_channel(tmp_path: Path) -> None:
    pairs = tmp_path / "pairs.json"
    pairs.write_text("{}")

    cmd, env, golds = _channel_cmd_env(
        "graft",
        full=False,
        sample=9,
        repo="django",
        pairs=(pairs,),
        workers=3,
    )

    assert "graft" in _EXTERNAL_CHANNELS
    assert cmd[cmd.index("--provider") + 1] == "graft"
    assert cmd[-6:] == ["--sample", "9", "--repo", "django", "--workers", "3"]
    assert env["EVAL_CHANNEL_LABEL"] == "graft"
    assert env["FITNESS_PAIRS"] == str(pairs)
    assert golds == [pairs]
