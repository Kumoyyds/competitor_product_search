from src.orchestrator.__main__ import _parser


def test_cli_new_and_rerun_vision_flags():
    new = _parser().parse_args(["new", "--input", "items.json"])
    assert new.vision is False
    rerun = _parser().parse_args(["rerun", "--batch-id", "b-1", "--vision"])
    assert rerun.vision_enabled is True
    inherited = _parser().parse_args(["rerun", "--batch-id", "b-1"])
    assert inherited.vision_enabled is None
