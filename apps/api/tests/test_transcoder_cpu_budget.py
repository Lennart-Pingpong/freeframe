"""A transcode has to be able to stay out of the way of everything else.

Unbounded, one transcode takes about 10 of 16 cores for the default ladder and
still 9 for a single rung -- so `TRANSCODER_QUALITIES` is not a way to get the
machine back, and a self-host has nothing to reach for. `TRANSCODER_CPU_LIMIT`
is that lever.

Two properties are worth pinning beyond the arithmetic:

  1. **Unset changes nothing.** The setting has to be invisible to every
     deployment that does not use it, down to the ffmpeg arguments.
  2. **A bad value does not stop transcoding.** It gets written when the machine
     is already struggling, so the cost of a typo must be a line in the log --
     not every upload failing through the retry ladder, which is the shape #325
     already had to fix once for the ladder itself.
"""
import pytest

from packages.transcoder.ffmpeg_transcoder import (
    available_cpus, get_cpu_budget, parse_cpu_budget, thread_plan,
)


# ------------------------------------------------------------- absolute counts

def test_a_core_count_is_taken_as_written():
    assert parse_cpu_budget("6", cpu_count=16) == 6


def test_surrounding_whitespace_is_tolerated():
    # Pasted out of a compose file, this is what an operator actually types.
    assert parse_cpu_budget("  6  ", cpu_count=16) == 6


def test_asking_for_every_core_is_the_same_as_asking_for_nothing(capsys):
    # Honesty rather than a no-op limit: someone who writes 16 on a 16-core box
    # believes a cap is in force, and would otherwise never learn it is not.
    assert parse_cpu_budget("16", cpu_count=16) is None
    assert "leaving CPU use unbounded" in capsys.readouterr().out


def test_asking_for_more_than_exists_is_also_unbounded(capsys):
    assert parse_cpu_budget("64", cpu_count=16) is None
    assert "64 of 16 available" in capsys.readouterr().out


# ------------------------------------------------------------------- shares

def test_a_percentage_is_read_against_what_is_available():
    assert parse_cpu_budget("50%", cpu_count=16) == 8


def test_a_percentage_is_rounded_not_truncated():
    # 35% of 16 is 5.6. Truncation would hand out 5 and quietly give the
    # operator less than they asked for, every time the share does not divide
    # evenly -- which is most of the time.
    assert parse_cpu_budget("35%", cpu_count=16) == 6
    assert parse_cpu_budget("40%", cpu_count=4) == 2      # 1.6
    # A share that lands exactly on a core is unaffected either way.
    assert parse_cpu_budget("50%", cpu_count=16) == 8


def test_a_percentage_never_rounds_down_to_nothing():
    # Separate from the rounding above, and separately mutable: on a small box
    # a share under half a core would otherwise resolve to zero threads, which
    # is not a quieter encode but a command ffmpeg rejects.
    assert parse_cpu_budget("1%", cpu_count=8) == 1
    assert parse_cpu_budget("10%", cpu_count=4) == 1      # 0.4


def test_a_comma_decimal_is_accepted():
    # A German-locale operator writes 12,5 and means twelve and a half.
    assert parse_cpu_budget("12,5%", cpu_count=16) == 2


def test_whitespace_inside_a_share_is_tolerated():
    assert parse_cpu_budget(" 25 % ", cpu_count=16) == 4


def test_a_full_share_is_unbounded(capsys):
    assert parse_cpu_budget("100%", cpu_count=16) is None
    assert "leaving CPU use unbounded" in capsys.readouterr().out


# ------------------------------------------------------------ the unset case

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_is_unbounded_and_says_nothing(raw, capsys):
    # Silence matters here: this is every deployment that never touched the
    # setting, and a line per transcode would be noise in all of them.
    assert parse_cpu_budget(raw, cpu_count=16) is None
    assert capsys.readouterr().out == ""


def test_get_cpu_budget_reads_the_environment(monkeypatch):
    # The core count is pinned rather than taken from whatever is running the
    # suite: a budget at or above what is available resolves to unbounded on
    # purpose, so 4 would mean "no limit" on a four-core CI runner and this
    # would fail for a reason that has nothing to do with reading the env.
    monkeypatch.setattr(
        "packages.transcoder.ffmpeg_transcoder.available_cpus", lambda: 16
    )
    monkeypatch.setenv("TRANSCODER_CPU_LIMIT", "4")
    assert get_cpu_budget() == 4
    monkeypatch.delenv("TRANSCODER_CPU_LIMIT")
    assert get_cpu_budget() is None


# --------------------------------------------------------------- typo cases

@pytest.mark.parametrize("raw", ["six", "6 cores", "abc%", "%", "--4"])
def test_an_unusable_value_falls_back_and_reports(raw, capsys):
    assert parse_cpu_budget(raw, cpu_count=16) is None
    assert "TRANSCODER_CPU_LIMIT" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["0", "-1", "0%", "-5%"])
def test_a_value_leaving_no_cores_falls_back_and_reports(raw, capsys):
    assert parse_cpu_budget(raw, cpu_count=16) is None
    assert "would leave no cores" in capsys.readouterr().out


# ------------------------------------------------------------- the thread plan

def test_no_budget_means_no_arguments():
    # The whole point of the unset case: the command has to come out byte for
    # byte as it did before this setting existed.
    assert thread_plan(None, 3) is None


def test_a_budget_is_split_across_the_rungs():
    per_rung, filter_threads = thread_plan(6, 3)
    assert per_rung == [2, 2, 2]
    assert sum(per_rung) == 6
    assert filter_threads == 1


def test_a_remainder_goes_to_the_largest_rungs():
    # Rungs are ordered largest first, and the largest is the slowest to encode,
    # so it is the one that should get the spare thread.
    per_rung, _ = thread_plan(7, 3)
    assert per_rung == [3, 2, 2]
    assert sum(per_rung) == 7


def test_a_single_rung_gets_the_whole_budget():
    per_rung, _ = thread_plan(6, 1)
    assert per_rung == [6]


def test_the_filter_graph_gets_one_thread_whatever_the_budget():
    # Measured, not assumed: holding the encoders at two threads each, capping
    # the graph at one cost nothing (13.7s against 13.5s unbounded). Leaving it
    # unbounded would put back the one pool this setting exists to bound.
    for budget in (1, 2, 6, 12):
        assert thread_plan(budget, 3)[1] == 1


def test_a_budget_below_the_rung_count_keeps_every_rung_alive(capsys):
    # Three rungs cannot run on two threads: a rung with zero threads is not a
    # slower rung, it is a broken command. The floor is honoured and reported
    # rather than silently exceeded.
    per_rung, _ = thread_plan(2, 3)
    assert per_rung == [1, 1, 1]
    out = capsys.readouterr().out
    assert "asks for 2 core(s)" in out
    assert "using 3" in out


def test_a_budget_that_fits_exactly_says_nothing(capsys):
    thread_plan(3, 3)
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------- availability

def test_available_cpus_is_at_least_one():
    # Whatever the sandbox, a number below one would make every downstream
    # share resolve to zero threads.
    assert available_cpus() >= 1


def test_a_share_defaults_to_what_is_available(monkeypatch):
    monkeypatch.setattr(
        "packages.transcoder.ffmpeg_transcoder.available_cpus", lambda: 8
    )
    assert parse_cpu_budget("25%") == 2


def test_a_cgroup_quota_wins_over_the_host_core_count(monkeypatch):
    # The case this exists for: a worker already capped at 4 of 16 with `cpus:`
    # asks for 50%. os.cpu_count() reports the *host's* cores inside a
    # container, so reading that would hand out 8 -- twice what the worker has,
    # and the operator would never see why the cap did nothing.
    import packages.transcoder.ffmpeg_transcoder as mod

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/sys/fs/cgroup/cpu.max":
            return "400000 100000\n"          # 4 cores' worth of quota
        raise OSError("not here")

    monkeypatch.setattr(mod.Path, "read_text", fake_read_text)
    assert mod.available_cpus() == 4
    assert mod.parse_cpu_budget("50%") == 2


def test_an_unlimited_cgroup_falls_through_to_the_real_core_count(monkeypatch):
    # "max" is what an uncapped container reads, and it must not be mistaken
    # for a quota of zero.
    import packages.transcoder.ffmpeg_transcoder as mod

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/sys/fs/cgroup/cpu.max":
            return "max 100000\n"
        raise OSError("not here")

    monkeypatch.setattr(mod.Path, "read_text", fake_read_text)
    monkeypatch.setattr(mod.os, "sched_getaffinity", lambda _pid: set(range(12)))
    assert mod.available_cpus() == 12


def test_a_sub_core_quota_still_yields_one(monkeypatch):
    # `cpus: "0.5"` is legal. Zero threads is not a slower encode, it is a
    # broken command.
    import packages.transcoder.ffmpeg_transcoder as mod

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/sys/fs/cgroup/cpu.max":
            return "50000 100000\n"
        raise OSError("not here")

    monkeypatch.setattr(mod.Path, "read_text", fake_read_text)
    assert mod.available_cpus() == 1
