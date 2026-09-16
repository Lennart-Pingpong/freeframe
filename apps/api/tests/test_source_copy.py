"""Stream copy has to reach the ffmpeg command, and only where it is allowed.

Two mistakes are possible here and both are silent. One is the setting doing
nothing, so an instance that turned it on still spends an hour per master. The
other is the opposite and worse: copying a source that should have been encoded,
which produces an asset that plays for nobody -- 10-bit H.264 reports the same
codec name as the 8-bit kind, and an HDR master copied without tone-mapping is
grey. So every test here reads the command that was actually built, and the ones
that must *not* copy outnumber the ones that must.
"""
import asyncio
import json
import os
from unittest.mock import MagicMock, patch

from packages.transcoder.base import TranscodeJob
from packages.transcoder.ffmpeg_transcoder import FFmpegTranscoder


_H264_8BIT = {"codec_name": "h264", "pix_fmt": "yuv420p"}


def _transcode(
    qualities: list[str],
    source: tuple[int, int] = (1920, 1080),
    video_stream: dict | None = None,
    audio_streams: list[dict] | None = None,
    source_copy: str | None = "true",
):
    """Run a transcode with everything mocked; return (hls command, result)."""
    width, height = source
    stream = {"r_frame_rate": "25/1", "duration": 6.0, "width": width, "height": height}
    stream.update(_H264_8BIT if video_stream is None else video_stream)

    def run(cmd, **_kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stderr = ""
        if "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "v:0":
            mock.stdout = json.dumps({"streams": [stream]})
        elif "-select_streams" in cmd and cmd[cmd.index("-select_streams") + 1] == "a":
            mock.stdout = json.dumps({"streams": audio_streams or []})
        return mock

    job = TranscodeJob(
        media_id="media-1", version_id="v1",
        input_s3_key="uploads/video.mp4", output_s3_prefix="hls/media-1/v1",
        qualities=qualities,
    )
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.com/uploads/video.mp4"

    env = {k: v for k, v in os.environ.items() if k != "TRANSCODER_SOURCE_COPY"}
    if source_copy is not None:
        env["TRANSCODER_SOURCE_COPY"] = source_copy

    with patch.dict(os.environ, env, clear=True), \
         patch("subprocess.run", side_effect=run) as mock_run, \
         patch("builtins.open", MagicMock()), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("pathlib.Path.rglob", return_value=[]), \
         patch("pathlib.Path.mkdir"), \
         patch("shutil.rmtree"):
        result = asyncio.run(FFmpegTranscoder(s3, "test-bucket").transcode(job))
        commands = [c[0][0] for c in mock_run.call_args_list]

    hls = [c for c in commands if "-f" in c and c[c.index("-f") + 1] == "hls"]
    assert len(hls) == 1, f"expected one HLS command, got {len(hls)}"
    return hls[0], result, commands


def _copies(cmd: list[str]) -> bool:
    return "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"


# ─────────────────────────────────── what may be copied

def test_a_browser_safe_source_at_the_ladder_s_size_is_remuxed():
    cmd, result, _ = _transcode(["1080p"], source=(1920, 1080))

    assert _copies(cmd)
    assert "-filter_complex" not in cmd, "a copy has nothing to filter"
    assert cmd[cmd.index("-var_stream_map") + 1] == "v:0"
    assert result.success and result.hls_prefix == "hls/media-1/v1"
    assert (result.width, result.height) == (1920, 1080)


def test_a_rung_above_the_source_copies_too():
    # `TRANSCODER_QUALITIES=1080p` on a 720p master: the ladder clamps the rung
    # to the source size, which is exactly the case this setting is for.
    cmd, _, _ = _transcode(["1080p"], source=(1280, 720))

    assert _copies(cmd)


def test_nothing_is_copied_unless_the_deployment_asks():
    cmd, _, _ = _transcode(["1080p"], source=(1920, 1080), source_copy=None)

    assert not _copies(cmd)
    assert "-filter_complex" in cmd
    # and an unrecognised value is not an invitation either
    cmd, _, _ = _transcode(["1080p"], source=(1920, 1080), source_copy="maybe")
    assert not _copies(cmd)


# ─────────────────────────────────── what may not

def test_a_configured_ladder_still_gets_every_rung_it_asked_for():
    cmd, _, _ = _transcode(["1080p", "720p", "360p"], source=(1920, 1080))

    assert not _copies(cmd)
    assert "split=3" in cmd[cmd.index("-filter_complex") + 1]


def test_a_rung_below_the_source_is_encoded():
    # The reviewer asked for a smaller rendition than the master. Copying would
    # hand them the master instead, which is the setting overriding the ladder
    # rather than following it.
    cmd, _, _ = _transcode(["720p"], source=(1920, 1080))

    assert not _copies(cmd)
    assert "scale=1280:720" in cmd[cmd.index("-filter_complex") + 1]


def test_ten_bit_h264_is_encoded():
    # High 10 carries codec_name "h264" and plays in no browser. Checking the
    # codec name alone would copy it and the asset would be black for everyone.
    cmd, _, _ = _transcode(
        ["1080p"], video_stream={"codec_name": "h264", "pix_fmt": "yuv420p10le"},
    )

    assert not _copies(cmd)


def test_a_source_in_another_codec_is_encoded():
    for codec in ("prores", "hevc", "vp9", "mpeg2video"):
        cmd, _, _ = _transcode(
            ["1080p"], video_stream={"codec_name": codec, "pix_fmt": "yuv420p"},
        )
        assert not _copies(cmd), f"{codec} must not be copied"


def test_an_hdr_source_is_encoded():
    # 8-bit 4:2:0 with a PQ transfer is unusual but possible, and copying it
    # skips the tone-mapping the filter graph exists for.
    cmd, _, _ = _transcode(
        ["1080p"],
        video_stream={**_H264_8BIT, "color_transfer": "smpte2084"},
    )

    assert not _copies(cmd)


def test_a_source_of_unknown_size_is_encoded():
    # Without dimensions there is nothing to compare the rung against, so the
    # "one rendition at source size" precondition cannot be established.
    cmd, _, _ = _transcode(["1080p"], source=(0, 0))

    assert not _copies(cmd)


# ─────────────────────────────────── audio, and what a copy does not produce

def test_aac_rides_along_and_anything_else_is_converted():
    cmd, _, _ = _transcode(["1080p"], audio_streams=[{"codec_name": "aac"}])
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[cmd.index("-var_stream_map") + 1] == "v:0,a:0"

    # PCM is what an NLE exports and HLS cannot carry it. Converting costs
    # seconds and leaves the picture untouched, which is the point.
    cmd, _, _ = _transcode(["1080p"], audio_streams=[{"codec_name": "pcm_s16le"}])
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert _copies(cmd)


def test_a_silent_source_maps_no_audio():
    cmd, _, _ = _transcode(["1080p"], audio_streams=[])

    assert "-c:a" not in cmd
    assert cmd.count("-map") == 1
    assert cmd[cmd.index("-var_stream_map") + 1] == "v:0"


def test_a_copied_source_writes_no_second_file_to_download():
    # The rendition is the uploaded stream, so the master the download falls
    # back to is the same picture: a download MP4 would be a third copy of it.
    _, result, commands = _transcode(["1080p"], source=(1920, 1080))

    assert result.mp4_key is None
    assert not [c for c in commands if any(str(a).endswith("download.mp4") for a in c)]

    # ... while an encoded ladder still gets one, or this test would pass on a
    # transcoder that had stopped building them at all.
    _, result, commands = _transcode(["1080p"], source=(1920, 1080), source_copy=None)
    assert result.mp4_key == "hls/media-1/v1/download.mp4"
