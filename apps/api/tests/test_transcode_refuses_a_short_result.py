"""A transcode that stops early must not be stored as a finished one.

The case behind this file, from a live instance: a 31:18 master came out as
1:37 of HLS, and the version was saved `ready` with the source's real duration
beside it. Nothing had gone wrong as far as the code could tell -- ffmpeg
exited 0 and the playlist carried `#EXT-X-ENDLIST` -- because exit code is the
only thing the transcoder looks at.

It is reachable whenever the input stops being readable at a frame boundary:
the demuxer cannot tell that from the end of the file. Measured on the real
master, truncated at three successive frame boundaries, exit code 0 each time
with 92.4s, 96.1s and 99.8s of output. Truncated one byte *inside* a frame it
exits 183, which is why this is rare -- and why, being rare, it went unnoticed
for two days and would have gone unnoticed longer.

Every claim these tests make has been checked by breaking the code and watching
the test go red; the mutations are listed in the pull request. Tests that hold
nothing in place are worse than no tests, because they are read as cover.
"""
import json

import pytest

from packages.transcoder.ffmpeg_transcoder import (
    TranscodeTruncated,
    hls_output_seconds,
    parse_probe_metadata,
    refuse_a_truncated_result,
)


def _variant(tmp_path, name: str, segment_seconds: list[float], endlist=True):
    """Write a playlist the way the HLS muxer writes one."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:3",
             "#EXT-X-PLAYLIST-TYPE:VOD"]
    for i, s in enumerate(segment_seconds):
        lines += [f"#EXTINF:{s:.6f},", f"seg_{i:03d}.ts"]
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    (d / "playlist.m3u8").write_text("\n".join(lines) + "\n")
    return d


# ----------------------------------------- which duration the check may use
#
# The subtle half of this feature. `#EXTINF` measures the video timeline; a
# container's duration is its longest stream. Comparing one against the other
# refuses intact files, and it does so silently and only on some containers,
# which is the worst shape a check can have.

def test_an_mp4_reports_the_same_duration_twice():
    # MP4, MOV and AVI carry a per-stream duration, so both numbers agree and
    # the distinction never shows. This is the common case and the reason the
    # bug below is easy to miss.
    meta = parse_probe_metadata({
        "streams": [{"duration": "600.0", "width": 1920, "height": 1080,
                     "r_frame_rate": "25/1"}],
        "format": {"duration": "600.0"},
    })
    assert meta.duration_seconds == pytest.approx(600.0)
    assert meta.video_duration_seconds == pytest.approx(600.0)


def test_matroska_reports_the_video_track_separately_from_the_file():
    # Measured on a real file built for this: a 20s picture with a 35s sine
    # muxed beside it. `ffprobe -select_streams v:0` on it gives exactly this --
    # no stream duration, DURATION as a tag, and the container reporting the
    # audio. An NLE rough cut and a MediaRecorder capture both produce it.
    meta = parse_probe_metadata({
        "streams": [{"width": 1920, "height": 1080, "r_frame_rate": "25/1",
                     "tags": {"DURATION": "00:00:20.023000000"}}],
        "format": {"duration": "35.023000"},
    })
    # What the player, the database and the comment timecodes mean by "long":
    assert meta.duration_seconds == pytest.approx(35.023)
    # What an EXTINF sum may be compared against:
    assert meta.video_duration_seconds == pytest.approx(20.023)


def test_a_language_suffixed_duration_tag_still_counts():
    # ffmpeg writes DURATION-eng when the track carries a language.
    meta = parse_probe_metadata({
        "streams": [{"width": 640, "height": 480, "r_frame_rate": "25/1",
                     "tags": {"language": "eng", "DURATION-eng": "00:01:00.000000000"}}],
        "format": {"duration": "90.0"},
    })
    assert meta.video_duration_seconds == pytest.approx(60.0)


def test_no_video_duration_anywhere_is_none_rather_than_the_container():
    # A Matroska remuxed by a tool that writes no DURATION tag. The container
    # number is still wrong for this purpose, so the field stays empty and the
    # check declines rather than guessing.
    meta = parse_probe_metadata({
        "streams": [{"width": 640, "height": 480, "r_frame_rate": "25/1"}],
        "format": {"duration": "90.0"},
    })
    assert meta.duration_seconds == pytest.approx(90.0)
    assert meta.video_duration_seconds is None


@pytest.mark.parametrize("tag", ["", "N/A", "00:00", "1:2:3:4", "not:a:time",
                                 "00:00:00.000000000"])
def test_an_unusable_duration_tag_is_ignored(tag):
    meta = parse_probe_metadata({
        "streams": [{"width": 640, "height": 480, "r_frame_rate": "25/1",
                     "tags": {"DURATION": tag}}],
        "format": {"duration": "90.0"},
    })
    assert meta.video_duration_seconds is None


def test_a_stream_duration_is_preferred_over_a_tag():
    # Both present: the field is authoritative, the tag is what a muxer wrote.
    meta = parse_probe_metadata({
        "streams": [{"duration": "600.0", "width": 1920, "height": 1080,
                     "r_frame_rate": "25/1",
                     "tags": {"DURATION": "00:00:20.000000000"}}],
        "format": {"duration": "600.0"},
    })
    assert meta.video_duration_seconds == pytest.approx(600.0)


# ------------------------------------------------------- reading the playlist

def test_the_length_comes_from_the_playlist_not_the_segment_count(tmp_path):
    # Segment length varies with the frame rate, and on the copy path it comes
    # from the source's GOP rather than -hls_time, so counting files and
    # multiplying would be wrong on exactly the path this bug was found on.
    _variant(tmp_path, "0", [2.9029, 1.935267, 1.935267, 0.433767])
    assert hls_output_seconds(tmp_path) == pytest.approx(7.207201)


def test_the_longest_variant_wins(tmp_path):
    # A ladder writes one playlist per rung. They should agree; if they do not,
    # the longest is the one that says how much of the source was read.
    #
    # The long one is deliberately *not* the first the glob returns: with the
    # longest also sorting first, an implementation that simply kept the first
    # playlist it read would pass this and lose a truncated rung.
    _variant(tmp_path, "0", [2.0, 2.0])
    _variant(tmp_path, "1", [2.0, 2.0, 2.0])
    assert hls_output_seconds(tmp_path) == pytest.approx(6.0)


def test_the_longest_variant_wins_in_either_order(tmp_path):
    # And the mirror, so neither "first" nor "last" passes by accident.
    _variant(tmp_path, "0", [2.0, 2.0, 2.0])
    _variant(tmp_path, "1", [2.0, 2.0])
    assert hls_output_seconds(tmp_path) == pytest.approx(6.0)


def test_a_directory_with_no_playlist_reads_as_none(tmp_path):
    assert hls_output_seconds(tmp_path) is None


def test_a_malformed_line_does_not_lose_the_rest(tmp_path):
    d = tmp_path / "0"
    d.mkdir()
    (d / "playlist.m3u8").write_text(
        "#EXTM3U\n#EXTINF:2.000000,\nseg_000.ts\n"
        "#EXTINF:not-a-number,\nseg_001.ts\n"
        "#EXTINF:2.000000,\nseg_002.ts\n#EXT-X-ENDLIST\n"
    )
    # Better to under-report by one segment than to raise here: the caller is
    # deciding whether to keep a transcode, and an exception would fail it for
    # the wrong reason.
    assert hls_output_seconds(d.parent) == pytest.approx(4.0)


# --------------------------------------------------------------- the refusal

def test_a_full_length_ladder_is_accepted(tmp_path):
    _variant(tmp_path, "0", [2.0] * 50)
    refuse_a_truncated_result(tmp_path, 100.0)


def test_the_live_case_is_refused(tmp_path):
    # The numbers that were actually stored: 97.2s of a 1877.7s master.
    _variant(tmp_path, "0", [1.935267] * 50 + [0.433767])
    assert hls_output_seconds(tmp_path) == pytest.approx(97.2, abs=0.05)
    with pytest.raises(TranscodeTruncated) as exc:
        refuse_a_truncated_result(tmp_path, 1877.709167, label="ffmpeg (copy)")
    message = str(exc.value)
    assert "5%" in message
    assert "1877.7s" in message
    assert "ffmpeg (copy)" in message


def test_an_intact_source_whose_audio_outlives_its_video_is_accepted(tmp_path):
    # The regression that matters most here: the ladder is complete, the file
    # is fine, and only the number it is compared against could refuse it.
    # Passing the container's 35.0s instead of the video's 20.0s makes this
    # raise at 57%.
    _variant(tmp_path, "0", [2.0] * 10 + [0.023])
    refuse_a_truncated_result(tmp_path, 20.023)


def test_a_last_segment_cut_short_is_not_a_truncation(tmp_path):
    # The muxer ends the last segment where the frames end, so an exact match
    # is not on offer. Measured drift on a complete ladder: +0.023s on the copy
    # path, -0.040s on the encode path, at 1, 10 and 40 minutes alike.
    _variant(tmp_path, "0", [2.0] * 49 + [1.5])
    refuse_a_truncated_result(tmp_path, 100.0)


def test_the_slack_is_a_flat_number_and_not_a_share(tmp_path):
    # Pinned from *both* sides, because a slack is only as good as its upper
    # bound: raising the floor is invisible to a test that only checks that
    # rounding is tolerated.
    #
    # 2s short of a 100s source is rounding and must pass; 5s short is not and
    # must fail. Together these hold the floor inside (2s, 5s].
    nah, kurz = tmp_path / "nah", tmp_path / "kurz"
    _variant(nah, "0", [2.0] * 49)              # 98.0s of 100s
    refuse_a_truncated_result(nah, 100.0)

    _variant(kurz, "0", [2.0] * 47 + [1.0])     # 95.0s of 100s
    with pytest.raises(TranscodeTruncated):
        refuse_a_truncated_result(kurz, 100.0)


def test_the_slack_does_not_grow_with_the_source(tmp_path):
    # The same 5s shortfall on a 40-minute master. A proportional term would
    # forgive up to 24s here and let a quarter-minute of missing video through
    # on a long file while catching it on a short one.
    _variant(tmp_path, "0", [2.0] * 1197 + [1.0])   # 2395.0s of 2400s
    with pytest.raises(TranscodeTruncated):
        refuse_a_truncated_result(tmp_path, 2400.0)


def test_no_playlist_at_all_is_deliberately_not_judged(tmp_path):
    # ffmpeg with an HLS muxer either writes a playlist or exits non-zero, so
    # an empty directory here means the output layout moved rather than that a
    # transcode was cut short -- and failing every asset over a renamed
    # directory is the worse error. The wiring tests below are what keep this
    # from being a quiet way for the whole check to stop applying.
    refuse_a_truncated_result(tmp_path, 100.0)


def test_an_unknown_source_duration_cannot_be_judged(tmp_path):
    # Either probing failed and reported it there, or the container publishes
    # no video-track duration. Refusing here would turn a missing number into a
    # failed asset -- and, through the reaper, into a deleted master.
    #
    # Honest about what this holds: `None` is pinned, because dropping the
    # guard makes the comparison raise TypeError. `0.0` takes the same branch
    # and cannot fail on its own, since no comparison against zero raises; it
    # is asserted as documentation of the contract, not as cover.
    _variant(tmp_path, "0", [2.0])
    refuse_a_truncated_result(tmp_path, None)
    refuse_a_truncated_result(tmp_path, 0.0)


def test_the_exception_is_a_runtime_error(tmp_path):
    # The attempt loop catches RuntimeError for hardware and remux fallbacks.
    # This has to be one of those to reach the handler at all -- and a distinct
    # type so that the handler can let it through instead of degrading to an
    # encoder that would read the same truncated input again.
    assert issubclass(TranscodeTruncated, RuntimeError)


# ------------------------------------------------------------- the wiring
#
# Four things have to stay true in the transcode itself, and none of them is
# visible to a test of the function alone:
#
#   * the encode path calls the check,
#   * the copy path calls it too -- the path the incident happened on,
#   * nothing is uploaded when it refuses, and
#   * a refusal is not absorbed by the fallback to the encoder.
#
# Each has its own test below, because each can be broken without the others
# noticing.

_FULL = object()   # "write a ladder as long as the source"


def _drive_a_transcode(
    tmp_path, written_seconds, source_seconds=600.0, *,
    copy_path=False, encode_written_seconds=_FULL, video_stream=None,
    format_duration=None,
):
    """Run the real transcode with a mocked ffmpeg that writes a real playlist.

    The mock does what ffmpeg does rather than only reporting that it did: it
    reads the output path out of the command it was handed and writes a
    playlist there. Without that, nothing in this file would notice the check
    being removed from the transcode -- which is the mistake that let the
    original bug ship, and the one an earlier version of this file repeated for
    the copy path.

    Returns `(result, s3)` so a caller can ask what was uploaded.
    """
    import asyncio
    import os
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from packages.transcoder.base import TranscodeJob
    from packages.transcoder.ffmpeg_transcoder import FFmpegTranscoder

    stream = video_stream if video_stream is not None else {
        "codec_name": "h264", "pix_fmt": "yuv420p", "r_frame_rate": "25/1",
        "duration": source_seconds, "width": 1920, "height": 1080,
    }
    fmt = {"duration": format_duration if format_duration is not None else source_seconds}

    def _write_playlist(cmd, seconds):
        target = Path(cmd[-1])                          # <work>/hls/%v/playlist.m3u8
        variant = Path(str(target.parent).replace("%v", "0"))
        variant.mkdir(parents=True, exist_ok=True)
        whole, rest = divmod(seconds, 2.0)
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-PLAYLIST-TYPE:VOD"]
        for i in range(int(whole)):
            lines += ["#EXTINF:2.000000,", f"seg_{i:03d}.ts"]
        if rest:
            lines += [f"#EXTINF:{rest:.6f},", f"seg_{int(whole):03d}.ts"]
        lines.append("#EXT-X-ENDLIST")                  # as ffmpeg leaves it
        (variant / "playlist.m3u8").write_text("\n".join(lines) + "\n")
        (variant / "seg_000.ts").write_bytes(b"\x47" * 188)

    def run(cmd, **_kwargs):
        mock = MagicMock()
        mock.returncode = 0          # ffmpeg is happy, which is the whole point
        mock.stderr = ""
        mock.stdout = ""
        if cmd[0] == "ffprobe":
            selected = (cmd[cmd.index("-select_streams") + 1]
                        if "-select_streams" in cmd else "")
            if selected != "v:0":
                mock.stdout = json.dumps({"streams": []})     # no audio
            elif "packet=pts_time,flags" in cmd:
                # The copy path's keyframe probe: a closed GOP every two
                # seconds, which is what makes a remux allowed at all.
                #
                # It answers per window. The transcoder probes the head and the
                # tail, and a mock that always replies with head keyframes makes
                # the tail window look like a 30-minute gap -- the copy is then
                # refused, the run quietly becomes an encode, and a test that
                # believes it is covering the copy path covers nothing. That is
                # the same class of mistake this whole file exists to catch, so
                # the tests below also assert that the copy actually ran.
                interval = cmd[cmd.index("-read_intervals") + 1]
                start_text, _, length_text = interval.partition("%+")
                start = float(start_text) if start_text else 0.0
                length = float(length_text)
                mock.stdout = json.dumps({"packets": [
                    {"pts_time": f"{start + t:.3f}", "flags": "K_"}
                    for t in range(0, int(length) + 1, 2)
                ]})
            else:
                mock.stdout = json.dumps({"streams": [stream], "format": fmt})
            return mock
        if cmd[0] == "ffmpeg" and "trace_headers" in cmd:
            # Every sync sample an IDR, no recovery points: copyable. The count
            # has to match the keyframes reported for the same window, or the
            # transcoder refuses the copy for having open-GOP sync samples and
            # the run becomes an encode.
            length = float(cmd[cmd.index("-t") + 1])
            mock.stderr = "\n".join(
                "[trace_headers @ 0x1] nal_unit_type: 5(IDR)"
                for _ in range(0, int(length) + 1, 2)
            )
            return mock
        if "-f" in cmd and cmd[cmd.index("-f") + 1] == "hls":
            # The encode path spells the codec per rung (`-c:v:0`), the copy
            # path plainly (`-c:v copy`), so match on the prefix rather than on
            # one exact spelling.
            is_copy = any(
                cmd[i].startswith("-c:v") and cmd[i + 1] == "copy"
                for i in range(len(cmd) - 1)
            )
            if is_copy or not copy_path:
                seconds = written_seconds
            elif encode_written_seconds is _FULL:
                seconds = source_seconds       # the fallback encoder succeeds
            else:
                seconds = encode_written_seconds
            _write_playlist(cmd, seconds)
        return mock

    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.com/in.mp4"
    job = TranscodeJob(
        media_id="m1", version_id="v1", input_s3_key="raw/in.mp4",
        output_s3_prefix="processed/m1/v1", qualities=["1080p"],
    )
    env = {"TRANSCODER_SOURCE_COPY": "1"} if copy_path else {}
    with patch.dict(os.environ, env, clear=False), \
            patch("subprocess.run", side_effect=run):
        result = asyncio.run(FFmpegTranscoder(s3, "bucket").transcode(job))
    return result, s3


def test_the_check_is_wired_into_the_encode_path(tmp_path):
    # Everything above tests the function; this tests that the transcode asks
    # it, on a run where ffmpeg exits 0 and leaves a properly closed playlist --
    # the live shape, in miniature.
    #
    # The assertion is on the result rather than on a raised exception because
    # `transcode` turns every exception into `TranscodeResult(success=False)`.
    # That is the contract the task reads: `not result.success` makes it raise,
    # and the task retries three times a minute apart, which for a read that
    # ended early is exactly the right remedy.
    result, _ = _drive_a_transcode(tmp_path, written_seconds=98.0, source_seconds=600.0)
    assert result.success is False
    assert "16%" in result.error
    assert "600.0s source" in result.error


def test_the_check_is_wired_into_the_copy_path(tmp_path):
    # The path the incident actually happened on, and the one a check can be
    # dropped from while every other test in this file stays green.
    result, _ = _drive_a_transcode(
        tmp_path, written_seconds=98.0, source_seconds=600.0, copy_path=True,
    )
    assert result.success is False
    assert "16%" in result.error
    assert "(copy)" in result.error


def test_a_refused_transcode_uploads_nothing(tmp_path):
    # The check runs before the ladder goes to the bucket, not after. If it
    # moved below the upload the transcode would still fail -- and a short
    # ladder would still be sitting in the store under the version's prefix.
    result, s3 = _drive_a_transcode(
        tmp_path, written_seconds=98.0, source_seconds=600.0, copy_path=True,
    )
    assert result.success is False
    assert s3.upload_file.call_count == 0
    assert s3.put_object.call_count == 0


def test_a_refused_copy_does_not_fall_back_to_the_encoder(tmp_path):
    # A remux that fails on its own terms is worth encoding instead (#372). A
    # remux that stopped early is worth *reading again*: the source is intact,
    # so a fresh read is the remedy and the task already retries. Falling back
    # here would re-read the same input the same way and cost hours doing it.
    #
    # The encoder in this run would produce a full-length ladder, so without
    # the `except TranscodeTruncated: raise` clause this transcode succeeds.
    result, _ = _drive_a_transcode(
        tmp_path, written_seconds=98.0, source_seconds=600.0, copy_path=True,
        encode_written_seconds=600.0,
    )
    assert result.success is False
    assert "the read ended early" in result.error


def test_a_full_length_transcode_passes_the_check(tmp_path):
    # The control. A ladder as long as its source must get past the check.
    # Asserting only on the absence of *this* failure keeps the test from
    # depending on what the surrounding mocks do afterwards.
    result, _ = _drive_a_transcode(tmp_path, written_seconds=600.0, source_seconds=600.0)
    assert "the read ended early" not in (result.error or "")


def test_a_matroska_with_an_audio_tail_is_not_refused_end_to_end(tmp_path):
    # Blocker 1 as the transcode sees it: a 600s picture in a file whose
    # container reports 900s because the audio runs on. The ladder is complete.
    # Comparing against the container's number refuses it at 67%.
    result, _ = _drive_a_transcode(
        tmp_path,
        written_seconds=600.0,
        source_seconds=600.0,
        video_stream={
            "codec_name": "h264", "pix_fmt": "yuv420p", "r_frame_rate": "25/1",
            "width": 1920, "height": 1080,
            "tags": {"DURATION": "00:10:00.000000000"},
        },
        format_duration=900.0,
    )
    assert "the read ended early" not in (result.error or "")
