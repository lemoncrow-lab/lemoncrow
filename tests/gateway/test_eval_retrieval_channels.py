from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.cli.commands.eval import _EXTERNAL_CHANNELS, _RETRIEVAL_CHANNELS, _channel_cmd_env


def test_shared_channel_selects_current_client_without_relabeling_backends(tmp_path):
    pairs = tmp_path / "pairs.json"
    pairs.write_text("{}")
    cmd, env, _ = _channel_cmd_env("lemoncrow-shared", full=True, sample=0, repo="", pairs=(pairs,))
    assert "lemoncrow-shared" in _RETRIEVAL_CHANNELS
    assert cmd[cmd.index("--provider") + 1] == "lemoncrow-shared"
    assert env["EVAL_CHANNEL_LABEL"] == "lemoncrow-shared"


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


def test_lemoncrow_retrieval_channels_pin_their_backends(monkeypatch, tmp_path: Path) -> None:
    pairs = tmp_path / "pairs.json"
    pairs.write_text("{}")
    # Host state must not silently relabel a benchmark channel.
    monkeypatch.setenv("LEMONCROW_ZOEKT_MODE", "off")
    monkeypatch.delenv("LEMONCROW_CODE_EMBEDDER", raising=False)

    def env_for(channel: str) -> dict[str, str]:
        _cmd, env, _golds = _channel_cmd_env(
            channel,
            full=True,
            sample=0,
            repo=None,
            pairs=(pairs,),
            workers=1,
        )
        return env

    lexical = env_for("lexical")
    assert lexical["LEMONCROW_EXPLORE_LEXICAL"] == "1"
    assert lexical["LEMONCROW_ZOEKT_MODE"] == "off"
    assert lexical["LEMONCROW_EXPLORE_SEMANTIC"] == "0"
    assert "LEMONCROW_CODE_EMBEDDER" not in lexical

    zoekt = env_for("zoekt")
    assert zoekt["LEMONCROW_EXPLORE_LEXICAL"] == "0"
    assert zoekt["LEMONCROW_ZOEKT_MODE"] == "installed"
    assert zoekt["LEMONCROW_EXPLORE_SEMANTIC"] == "0"
    assert "LEMONCROW_CODE_EMBEDDER" not in zoekt

    semantic = env_for("semantic")
    assert semantic["LEMONCROW_EXPLORE_LEXICAL"] == "0"
    assert semantic["LEMONCROW_ZOEKT_MODE"] == "off"
    assert semantic["LEMONCROW_EXPLORE_SEMANTIC"] == "1"
    assert semantic["LEMONCROW_CODE_EMBEDDER"] == "bge"

    hybrid = env_for("lexical+zoekt")
    assert hybrid["LEMONCROW_EXPLORE_LEXICAL"] == "1"
    assert hybrid["LEMONCROW_ZOEKT_MODE"] == "installed"
    assert hybrid["LEMONCROW_EXPLORE_SEMANTIC"] == "0"
    assert "LEMONCROW_CODE_EMBEDDER" not in hybrid

    full = env_for("lexical+zoekt+semantic")
    assert full["LEMONCROW_EXPLORE_LEXICAL"] == "1"
    assert full["LEMONCROW_ZOEKT_MODE"] == "installed"
    assert full["LEMONCROW_EXPLORE_SEMANTIC"] == "1"
    assert full["LEMONCROW_CODE_EMBEDDER"] == "bge"
