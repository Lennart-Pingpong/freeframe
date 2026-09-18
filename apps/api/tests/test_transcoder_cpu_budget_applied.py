"""The budget has to reach an actual ffmpeg command, in the right position.

`parse_cpu_budget` and `thread_plan` can both be perfect and the feature still
dead, or worse, quietly dead. The position is not a detail here: a global
`-threads` before `-i` is an input option, it reaches the decoder, and the
encoders never see it. Measured on a 16-core host, three rungs, one run per
value -- 1, 2, 4, 6 and 8 all left occupancy at ~11 cores, exactly as if nothing
had been set. A limit that appears to work and does not is the worst outcome
this feature can have, so the placement is pinned, not just the presence.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.api.tests.test_source_copy import _transcode
from packages.transcoder.base import TranscodeJob
from packages.transcoder.ffmpeg_transcoder import FFmpegTranscoder


@pytest.fixture(autouse=True)
def _a_machine_of_a_known_size(monkeypatch):
    """Pin the core count so these tests mean the same thing everywhere.

    Without it they pass on a roomy workstation and fail on a small CI runner,
    for a reason that is the feature working: a budget at or above what is
    available deliberately resolves to unbounded, so `TRANSCODER_CPU_LIMIT=6`
    means "no limit" on a four-core box. Right behaviour, wrong thing for a test
    to depend on -- what is under test here is the arithmetic and the
    placement, not the size of the machine running pytest.
    """
    monkeypatch.setattr(
        "packages.transcoder.ffmpeg_transcoder.available_cpus", lambda: 16
    )


def _ffmpeg_cmd_for(qualities: list[str], source=(1920, 1080)) -> list[str]:
    """Run a transcode with everything mocked and return the ffmpeg command."""
    width, height = source

    def run(cmd, **_kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stderr = ""
        if "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "v:0":
            mock.stdout = json.dumps({"streams": [
                {"r_frame_rate": "25/1", "duration": 6.0, "width": width, "height": height},
            ]})
        elif "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "a":
            mock.stdout = json.dumps({"streams": []})
        return mock

    job = TranscodeJob(
        media_id="media-1", version_id="v1",
        input_s3_key="uploads/video.mp4", output_s3_prefix="hls/media-1/v1",
        qualities=qualities,
    )
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.com/uploads/video.mp4"
    with patch("subprocess.run", side_effect=run) as mock_run, \
         patch("builtins.open", MagicMock()), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("pathlib.Path.rglob", return_value=[]), \
         patch("pathlib.Path.mkdir"), \
         patch("shutil.rmtree"):
        asyncio.run(FFmpegTranscoder(s3, "test-bucket").transcode(job))
    calls = [c for c in mock_run.call_args_list
             if any("filter_complex" in str(a) for a in c[0][0])]
    assert calls, "no -filter_complex call was made"
    return calls[0][0][0]


def _threads_for(cmd: list[str]) -> dict[str, str]:
    """Every `-threads:v:i` in the command, by stream specifier."""
    return {
        token: cmd[i + 1]
        for i, token in enumerate(cmd)
        if token.startswith("-threads:v:")
    }


# --------------------------------------------------------- the unset case

def test_without_the_setting_the_command_is_unchanged(monkeypatch):
    # Every deployment that never touched this has to get byte-identical
    # arguments. No thread option of any kind may appear.
    monkeypatch.delenv("TRANSCODER_CPU_LIMIT", raising=False)
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert not [t for t in cmd if "threads" in t]


# ------------------------------------------------------------ the placement

def test_the_budget_never_becomes_a_global_option(monkeypatch):
    # The mutation this exists for: "simplify" the per-stream options into one
    # global `-threads`. It parses, it runs, it reads like a limit -- and it
    # caps nothing, because before `-i` it is a decoder option.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "6")
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert "-threads" not in cmd, "a bare -threads reaches the decoder, not the encoders"

    input_at = cmd.index("-i")
    for i, token in enumerate(cmd):
        if token.startswith("-threads:v:"):
            assert i > input_at, f"{token} sits before -i and would be an input option"


# --------------------------------------------------------- the split itself

def test_the_budget_is_divided_across_the_rungs(monkeypatch):
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "6")
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert _threads_for(cmd) == {
        "-threads:v:0": "2", "-threads:v:1": "2", "-threads:v:2": "2",
    }


def test_a_single_rung_receives_the_whole_budget(monkeypatch):
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "6")
    cmd = _ffmpeg_cmd_for(["720p"])

    assert _threads_for(cmd) == {"-threads:v:0": "6"}


def test_a_share_is_resolved_against_what_is_available(monkeypatch):
    monkeypatch.setattr(
        "packages.transcoder.ffmpeg_transcoder.available_cpus", lambda: 16
    )
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "25%")
    cmd = _ffmpeg_cmd_for(["1080p", "720p"])

    # 25% of 16 is 4, two rungs, two each.
    assert _threads_for(cmd) == {"-threads:v:0": "2", "-threads:v:1": "2"}


def test_every_rung_keeps_a_thread_when_the_budget_is_smaller(monkeypatch):
    # A rung with zero threads is not a slower rung, it is a broken command.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "2")
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert _threads_for(cmd) == {
        "-threads:v:0": "1", "-threads:v:1": "1", "-threads:v:2": "1",
    }


# --------------------------------------------------------- the filter graph

def test_the_filter_graph_is_bounded_too(monkeypatch):
    # Measured: with the encoders at two threads each, capping the graph at one
    # cost nothing (13.7s against 13.5s unbounded, 4.7 cores against 4.8).
    # Leaving it out would put back the one unbounded pool this setting exists
    # to remove.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "6")
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert cmd[cmd.index("-filter_complex_threads") + 1] == "1"


# ------------------------------------------------------- the stream-copy path

def test_a_stream_copy_gets_no_thread_arguments(monkeypatch):
    # A copy has no encoder and no filter graph, so there is nothing to bound.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "2")
    cmd, result, _ = _transcode(["1080p"], source=(1920, 1080))

    assert result.success
    assert not [t for t in cmd if "threads" in t]


def test_a_stream_copy_cannot_be_short_of_threads(monkeypatch, capsys):
    # Worth pinning because it is the reason the plan can be computed before the
    # path is chosen: a copy only happens once the ladder has resolved to a
    # single rung, and one rung is never short. If the copy gate ever widens to
    # a real ladder, this fails and the plan has to move.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "1")
    capsys.readouterr()

    cmd, result, _ = _transcode(["1080p"], source=(1920, 1080))

    assert result.success
    assert "need one thread each" not in capsys.readouterr().out


def test_a_copy_that_falls_back_to_encoding_still_gets_the_budget(monkeypatch):
    # The reason the plan cannot simply be skipped for a copy job: a refused or
    # failed copy falls back to a real encode, and that encode has to be capped
    # like any other. Keyframes 20s apart are refused for a copy.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "6")
    cmd, result, _ = _transcode(["1080p"], source=(1920, 1080),
                                keyframes=[0.0, 20.0])

    assert result.success
    assert _threads_for(cmd) == {"-threads:v:0": "6"}


def test_a_broken_value_leaves_the_command_alone(monkeypatch):
    # The setting gets written when the machine is already struggling. A typo
    # must cost a line in the log, not every upload -- the same failure shape
    # #325 had to fix once for the ladder itself.
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "half")
    cmd = _ffmpeg_cmd_for(["1080p", "720p", "360p"])

    assert not [t for t in cmd if "threads" in t]
    assert "-filter_complex" in cmd  # and it still built a real command
