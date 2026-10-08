# client/modules/data_stall_watch.py
"""
Self-calibrating data-cessation detector (WP 1.2 — the primary hang fix).

The client must decide, during a run, whether the tester is still producing measurement data
or has stalled — WITHOUT judging by how long a case takes (genuine cases run long) and WITHOUT
any user-entered duration (the user cannot know in advance how long a case or call will take).

So this detector uses only DATA, and it learns the run's OWN rhythm at runtime:

  - The caller feeds it, each monitor-loop iteration, the current combined data-progress value
    (WS stream frames + new Qi packets — see test_manager._data_progress_value), plus whether
    the app is BUSY and whether an operator pop-up is pending.
  - Every time the data value advances, the gap since the previous advance is recorded as a
    sample of this run's healthy inter-data cadence; the largest such gap seen so far is the
    reference. There is NO hard-coded or configured time.
  - A stall is SUSPECTED only when data has been flat for far longer than that observed
    healthy cadence (a generous internal safety multiple), while the app is BUSY and no
    operator pop-up is pending (coil-placement waits legitimately produce no data, so the
    detector pauses for them).

This class is deliberately pure and clock-free (the caller passes the timestamp), so it is
deterministic and unit-testable by replaying synthetic sample sequences. The final confirmation
(an active probe that the app really produced no new data) is the caller's job — kept out of
here so the decision logic stays testable.

The two constants below are internal engineering margins, NOT user configuration. They are set
conservatively so a data-producing case is NEVER aborted, and are validated/tuned against real
run data (the WP-1.0 heartbeat logs the actual healthy cadence). They are intentionally not
exposed in grl_config.json — there is nothing here for a user to guess.
"""


class DataStallWatch:
    """Flags SUSPECTED measurement-data cessation, self-calibrated to the run's own cadence."""

    #: How many multiples of the largest observed healthy inter-data gap must pass with NO new
    #: data before a stall is suspected. Generous on purpose — the cost of firing late is a
    #: slightly longer (still bounded) wait; the cost of firing early is aborting a healthy run,
    #: which must never happen. Tuned against real run cadence (WP 1.0 heartbeat), not a config.
    CADENCE_MULTIPLE = 10

    #: Don't arm until we've seen this many genuine data advances, so a tiny early baseline
    #: (a few ultra-fast frames) can't produce a too-small threshold. A count, not a time.
    MIN_BASELINE_INCREMENTS = 5

    #: How long the application may stay quiet AFTER a case reports `Ended` before that counts
    #: as a stall. Once a case has ended nothing is running, and the app is writing its report —
    #: expected silence, not a hang. Three MPP-TPR WS runs (EPP twice, EPP5 once) were aborted
    #: 6 s into exactly that window and lost their final report; BPP survived only because its
    #: finalisation happened to fit. Bounded, so a finalisation that never completes is still
    #: caught rather than hanging forever.
    FINALISE_GRACE_S = 120.0

    def __init__(self):
        self._last_value = None       # last data-progress value seen
        self._last_change_t = None    # timestamp of the last data advance (or pop-up reset)
        self._max_gap = 0.0           # largest healthy inter-data gap observed this run
        self._increments = 0          # number of genuine data advances (baseline confidence)

    # -- introspection for the heartbeat (WP 1.0/1.5) -----------------------
    @property
    def data_value(self):
        """Last data-progress value observed (None before the first sample)."""
        return self._last_value

    def flat_seconds(self, now: float) -> float:
        """Seconds since data last advanced (0 before the first sample)."""
        if self._last_change_t is None:
            return 0.0
        return max(0.0, now - self._last_change_t)

    @property
    def observed_cadence(self) -> float:
        """Largest healthy inter-data gap observed so far this run (0 until learned)."""
        return self._max_gap

    @property
    def armed(self) -> bool:
        """Whether enough baseline exists to judge a stall at all."""
        return self._increments >= self.MIN_BASELINE_INCREMENTS and self._max_gap > 0

    # -- the decision -------------------------------------------------------
    def update(self, now: float, data_value: int, app_busy: bool, popup_pending: bool,
               case_ended: bool = False) -> bool:
        """
        Feed one observation. Returns True iff a data-cessation is now SUSPECTED (the caller
        should then actively confirm before aborting). Never raises.
        """
        # First sample — just record the starting point.
        if self._last_value is None:
            self._last_value = data_value
            self._last_change_t = now
            return False

        # Operator pop-up pending: a legitimate no-data period (e.g. coil placement). Do not
        # accrue stall time and do not sample cadence — just hold the clock at 'now'.
        if popup_pending:
            self._last_change_t = now
            self._last_value = data_value
            return False

        # Data advanced → the tester is alive. Record the healthy gap and reset the clock.
        if data_value != self._last_value:
            gap = now - self._last_change_t
            if gap > self._max_gap:
                self._max_gap = gap
            self._last_value = data_value
            self._last_change_t = now
            self._increments += 1
            return False

        # Data is flat, and the case has already reported `Ended`: the tester has stopped
        # because the case finished, and the application is writing its report. Expected
        # quiet, not cessation. The clock is deliberately NOT reset here — that is what
        # bounds the wait, so a finalisation that never completes still falls through to
        # the judgement below instead of hanging forever.
        if case_ended and (now - self._last_change_t) <= self.FINALISE_GRACE_S:
            return False

        # Data is flat. Only a fully-armed, BUSY, pop-up-free state can suspect a stall.
        if not self.armed or not app_busy:
            return False
        flat_for = now - self._last_change_t
        return flat_for > self._max_gap * self.CADENCE_MULTIPLE
