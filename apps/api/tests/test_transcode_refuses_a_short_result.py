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
"""
import pytest

from packages.transcoder.ffmpeg_transcoder import (
    TranscodeTruncated,
    hls_output_seconds,
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
    _variant(tmp_path, "0", [1.935267] * 48 + [0.433767])
    with pytest.raises(TranscodeTruncated) as exc:
        refuse_a_truncated_result(tmp_path, 1877.709167, label="ffmpeg (copy)")
    message = str(exc.value)
    assert "5%" in message
    assert "1877.7s" in message
    assert "ffmpeg (copy)" in message


def test_a_last_segment_cut_short_is_not_a_truncation(tmp_path):
    # The muxer ends the last segment where the frames end, so an exact match
    # is not on offer. Half a second short of a 100s source is rounding.
    _variant(tmp_path, "0", [2.0] * 49 + [1.5])
    refuse_a_truncated_result(tmp_path, 100.0)


def test_the_slack_is_absolute_on_a_short_source(tmp_path):
    # One percent of a 10s clip is 0.1s, which is less than one frame. The
    # floor is what keeps a short asset from failing on rounding alone.
    _variant(tmp_path, "0", [2.0] * 4)          # 8.0s of a 10s source
    refuse_a_truncated_result(tmp_path, 10.0)


def test_the_slack_is_proportional_on_a_long_source(tmp_path):
    # Three seconds short of a 40-minute master is rounding; forty is not.
    # Two directories rather than two variants in one: the reader takes the
    # longest variant it finds, so a second playlist beside the first would
    # measure the first one twice.
    knapp, kurz = tmp_path / "knapp", tmp_path / "kurz"
    _variant(knapp, "0", [2.0] * 1198)          # 2396s of 2400s
    refuse_a_truncated_result(knapp, 2400.0)

    _variant(kurz, "0", [2.0] * 1180)           # 2360s of 2400s
    with pytest.raises(TranscodeTruncated):
        refuse_a_truncated_result(kurz, 2400.0)


def test_no_playlist_at_all_is_deliberately_not_judged(tmp_path):
    # ffmpeg with an HLS muxer either writes a playlist or exits non-zero, so
    # an empty directory here means the output layout moved rather than that a
    # transcode was cut short -- and failing every asset over a renamed
    # directory is the worse error. The wiring test below is what keeps this
    # from being a quiet way for the whole check to stop applying.
    refuse_a_truncated_result(tmp_path, 100.0)


def test_an_unknown_source_duration_cannot_be_judged(tmp_path):
    # Probing failed earlier and reported it there. Refusing here would turn
    # one missing number into a failed asset, so this stays quiet.
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

def _drive_a_transcode(tmp_path, written_seconds: float, source_seconds: float = 600.0):
    """Run the real transcode with a mocked ffmpeg that writes a real playlist.

    The mock does what ffmpeg does rather than only reporting that it did: it
    reads the output path out of the command it was handed and writes a
    playlist there. Without that, nothing in this file would notice the check
    being removed from the transcode -- which is the mistake that let the
    original bug ship.
    """
    import asyncio
    import json
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from packages.transcoder.base import TranscodeJob
    from packages.transcoder.ffmpeg_transcoder import FFmpegTranscoder

    def run(cmd, **_kwargs):
        mock = MagicMock()
        mock.returncode = 0          # ffmpeg is happy, which is the whole point
        mock.stderr = ""
        mock.stdout = ""
        if cmd[0] == "ffprobe":
            selected = (cmd[cmd.index("-select_streams") + 1]
                        if "-select_streams" in cmd else "")
            if selected == "v:0":
                mock.stdout = json.dumps({"streams": [{
                    "codec_name": "h264", "pix_fmt": "yuv420p",
                    "r_frame_rate": "25/1", "duration": source_seconds,
                    "width": 1920, "height": 1080,
                }]})
            else:
                mock.stdout = json.dumps({"streams": []})
            return mock
        if "-f" in cmd and cmd[cmd.index("-f") + 1] == "hls":
            target = Path(cmd[-1])                      # <work>/hls/%v/playlist.m3u8
            variant = Path(str(target.parent).replace("%v", "0"))
            variant.mkdir(parents=True, exist_ok=True)
            whole, rest = divmod(written_seconds, 2.0)
            lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-PLAYLIST-TYPE:VOD"]
            for i in range(int(whole)):
                lines += ["#EXTINF:2.000000,", f"seg_{i:03d}.ts"]
            if rest:
                lines += [f"#EXTINF:{rest:.6f},", f"seg_{int(whole):03d}.ts"]
            lines.append("#EXT-X-ENDLIST")              # as ffmpeg leaves it
            (variant / "playlist.m3u8").write_text("\n".join(lines) + "\n")
        return mock

    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.example.com/in.mp4"
    job = TranscodeJob(
        media_id="m1", version_id="v1", input_s3_key="raw/in.mp4",
        output_s3_prefix="processed/m1/v1", qualities=["1080p"],
    )
    with patch("subprocess.run", side_effect=run):
        return asyncio.run(FFmpegTranscoder(s3, "bucket").transcode(job))


def test_the_check_is_wired_into_the_transcode(tmp_path):
    # The one test in this file that fails if the call is deleted from the
    # attempt loop. Everything above tests the function; this tests that the
    # transcode asks it, on a run where ffmpeg exits 0 and leaves a properly
    # closed playlist -- the live shape, in miniature.
    #
    # The assertion is on the result rather than on a raised exception because
    # `transcode` turns every exception into `TranscodeResult(success=False)`.
    # That is the contract the task reads: `not result.success` makes it raise,
    # and the task retries three times a minute apart, which for a read that
    # ended early is exactly the right remedy.
    result = _drive_a_transcode(tmp_path, written_seconds=98.0, source_seconds=600.0)
    assert result.success is False
    assert "16%" in result.error
    assert "600.0s source" in result.error


def test_a_full_length_transcode_passes_the_check(tmp_path):
    # The control. A ladder as long as its source must get past the check.
    # Asserting only on the absence of *this* failure keeps the test from
    # depending on what the surrounding mocks do afterwards.
    result = _drive_a_transcode(tmp_path, written_seconds=600.0, source_seconds=600.0)
    assert "the read ended early" not in (result.error or "")
