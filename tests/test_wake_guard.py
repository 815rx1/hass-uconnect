"""Tests for the per-vehicle wake-up history and its limits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.uconnect.wake_guard import (
    AUTO_REFRESH_MAX_FAILURES,
    AUTO_REFRESH_MAX_PER_DAY,
    AUTO_REFRESH_MAX_WITHOUT_PROGRESS,
    AUTO_REFRESH_MIN_SPACING,
    HISTORY_WINDOW,
    MANUAL_MAX_PER_DAY,
    MANUAL_MAX_PER_HOUR,
    MANUAL_REFRESH_COOLDOWN,
    MANUAL_REFRESH_MAX_PER_DAY,
    SESSION_MERGE_WINDOW,
    STARTUP_GRACE,
    STORAGE_KEY,
    AlreadyDone,
    Blocked,
    Decision,
    WakeGuard,
    WakeRecord,
)

VIN = "ZFAEFAC39MX000001"
# Close to the real clock: some guard calls take the time themselves
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
INTERVAL = timedelta(minutes=30)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def new_guard(hass) -> WakeGuard:
    guard = WakeGuard(hass)
    await guard.async_load()
    # Started a day ago, unless a test is about the start itself
    guard._started = monotonic() - timedelta(days=1).total_seconds()
    return guard


def begin(guard: WakeGuard, minute: float, interval=INTERVAL, adaptive=False):
    """Ask for an automatic refresh.

    Unless adaptive, the step is set to the interval, so the interval is what
    applies, as when refreshes found no new percent at shorter steps.
    """
    if interval is not None and not adaptive:
        guard.session(VIN).step_minutes = int(interval.total_seconds() // 60)
    return guard.begin_auto_refresh(VIN, "DEEPREFRESH", interval, at(minute))


async def test_without_interval_only_the_start_refresh(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    assert begin(guard, 3, None).decision is Decision.ALLOWED
    for minute in range(18, 24 * 60, 15):
        assert begin(guard, minute, None).decision is Decision.SESSION_DONE
    assert guard.auto_refreshes_last_day(VIN, at(24 * 60)) == 1

    # The next charge gets its own
    guard.charging_stopped(VIN, at(24 * 60))
    assert guard.charging_started(VIN, at(25 * 60)) is True
    assert begin(guard, 25 * 60 + 3, None).decision is Decision.ALLOWED


async def test_startup_grace(hass):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = WakeGuard(hass)
        await guard.async_load()
        guard.charging_started(VIN, at(0))

        clock[0] += 180
        decision = begin(guard, 3)
        assert decision.decision is Decision.WAIT
        assert decision.wait == STARTUP_GRACE - timedelta(minutes=3)
        clock[0] += 720
        assert begin(guard, 15).decision is Decision.ALLOWED


async def test_startup_grace_survives_a_clock_step(hass):
    # Booted with a clock two hours behind, which NTP then corrected
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = WakeGuard(hass)
        await guard.async_load()
        guard.charging_started(VIN, at(0))

        clock[0] += 60
        decision = begin(guard, 121)
        assert decision.decision is Decision.WAIT
        assert decision.wait == STARTUP_GRACE - timedelta(minutes=1)


async def test_spacing_after_any_command(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))

    # The user locked the car a minute ago: no automatic wake-up yet
    guard.record_command(VIN, "RDL", at(0))
    decision = begin(guard, 1)
    assert decision.decision is Decision.WAIT
    assert decision.wait == AUTO_REFRESH_MIN_SPACING - timedelta(minutes=1)

    assert begin(guard, 15).decision is Decision.ALLOWED


async def test_interval_is_shared_by_all_entries_of_a_vehicle(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    assert begin(guard, 3).decision is Decision.ALLOWED

    # A second entry for the same car asks 15 minutes later
    decision = begin(guard, 18)
    assert decision.decision is Decision.WAIT
    assert decision.wait == timedelta(minutes=15)

    assert begin(guard, 33).decision is Decision.ALLOWED


async def test_daily_limit_counts_automatic_refreshes(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    interval = timedelta(minutes=15)
    allowed = []
    for minute in range(24 * 60):
        if begin(guard, minute, interval).decision is Decision.ALLOWED:
            allowed.append(minute)
            # Each refresh brings a gain, so only the daily limit applies
            guard.auto_refresh_done(VIN, float(len(allowed)), at(minute))

    assert len(allowed) == AUTO_REFRESH_MAX_PER_DAY
    # The next one waits until the first leaves the window
    assert begin(guard, 24 * 60, interval).decision is Decision.ALLOWED


async def test_no_progress_ends_the_session(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    minute = 0
    for _ in range(1 + AUTO_REFRESH_MAX_WITHOUT_PROGRESS):
        minute += 30
        assert begin(guard, minute).decision is Decision.ALLOWED
        guard.auto_refresh_done(VIN, 80.0, at(minute))

    assert begin(guard, minute + 30).decision is Decision.SESSION_DONE


async def test_unknown_charge_counts_as_no_progress(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    for i in range(AUTO_REFRESH_MAX_WITHOUT_PROGRESS):
        assert begin(guard, 30 * (i + 1)).decision is Decision.ALLOWED
        guard.auto_refresh_done(VIN, None, None)
    assert begin(guard, 200).decision is Decision.SESSION_DONE


async def test_upload_time_that_does_not_move_is_no_progress(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    uploaded = at(-5)
    soc = 50.0
    for i in range(1 + AUTO_REFRESH_MAX_WITHOUT_PROGRESS):
        assert begin(guard, 30 * (i + 1)).decision is Decision.ALLOWED
        # The charge appears to rise, but the vehicle sent nothing new
        soc += 1
        guard.auto_refresh_done(VIN, soc, uploaded)
    assert begin(guard, 200).decision is Decision.SESSION_DONE


async def test_failures_end_the_session_until_reset(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    for i in range(AUTO_REFRESH_MAX_FAILURES):
        assert begin(guard, 30 * (i + 1)).decision is Decision.ALLOWED
        guard.auto_refresh_failed(VIN)
    assert begin(guard, 200).decision is Decision.SESSION_DONE

    # A new PIN lets it try again
    guard.reset_failures(VIN)
    assert begin(guard, 230).decision is Decision.ALLOWED


async def test_flapping_charge_stays_one_session(hass):
    guard = await new_guard(hass)
    assert guard.charging_started(VIN, at(0)) is True
    assert begin(guard, 3).decision is Decision.ALLOWED
    guard.auto_refresh_done(VIN, 50.0, at(3))

    # Solar surplus charging: stops and resumes every few minutes
    for minute in range(10, 120, 10):
        guard.charging_stopped(VIN, at(minute))
        assert guard.charging_started(VIN, at(minute + 5)) is False
    assert guard.session(VIN).auto_refreshes == 1

    # A real break starts a new session
    guard.charging_stopped(VIN, at(200))
    later = 200 + SESSION_MERGE_WINDOW.total_seconds() / 60 + 1
    assert guard.charging_started(VIN, at(later)) is True
    assert guard.session(VIN).auto_refreshes == 0


async def test_automatic_command_keeps_all_limits(hass):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = WakeGuard(hass)
        await guard.async_load()

        clock[0] += 180
        with pytest.raises(Blocked) as err:
            guard.check_automatic_command(VIN, "VF", at(3))
        assert err.value.wait == STARTUP_GRACE - timedelta(minutes=3)
        clock[0] += 720

        # Locked a minute ago, which a person sending it would not wait for
        guard.record_command(VIN, "RDL", at(14))
        guard.check_manual_command(VIN, "VF", at(15))
        with pytest.raises(Blocked):
            guard.check_automatic_command(VIN, "VF", at(15))
        guard.check_automatic_command(VIN, "VF", at(20))

        # and the limits of commands sent on request
        guard.record_command(VIN, "VF", at(20))
        with pytest.raises(Blocked) as err:
            guard.check_automatic_command(VIN, "VF", at(26))
        assert err.value.key == "refresh_cooldown"


async def test_manual_refresh_cooldown_and_daily_limit(hass):
    guard = await new_guard(hass)
    guard.check_manual_command(VIN, "DEEPREFRESH", at(0))
    guard.record_command(VIN, "DEEPREFRESH", at(0))

    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "VF", at(4))
    assert err.value.key == "refresh_cooldown"
    assert err.value.wait == MANUAL_REFRESH_COOLDOWN - timedelta(minutes=4)

    # Other commands are not held back by the refresh cooldown
    guard.check_manual_command(VIN, "RDL", at(1))
    guard.check_manual_command(VIN, "RDU", at(1))

    # Just past the cooldown each time, 48 of them fit into a day
    step = MANUAL_REFRESH_COOLDOWN.total_seconds() / 60 + 1
    minute = 0
    for _ in range(MANUAL_REFRESH_MAX_PER_DAY - 1):
        minute += step
        guard.check_manual_command(VIN, "DEEPREFRESH", at(minute))
        guard.record_command(VIN, "DEEPREFRESH", at(minute))
    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "DEEPREFRESH", at(minute + step))
    assert err.value.key == "refresh_daily_limit"


async def test_automatic_refresh_counts_for_the_manual_cooldown(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    assert begin(guard, 3).decision is Decision.ALLOWED
    with pytest.raises(Blocked):
        guard.check_manual_command(VIN, "DEEPREFRESH", at(8))


async def test_repeated_confirmed_command(hass):
    guard = await new_guard(hass)
    record = guard.record_command(VIN, "RDU", at(0))

    # Not confirmed yet, a retry is fine
    guard.check_manual_command(VIN, "RDU", at(0.5))

    guard.command_confirmed(record, at(0.1))
    with pytest.raises(AlreadyDone):
        guard.check_manual_command(VIN, "RDU", at(0.5))
    # The same one a minute later goes out
    guard.check_manual_command(VIN, "RDU", at(1.2))

    # So does it after the opposite was sent in between
    guard.command_confirmed(guard.record_command(VIN, "RDL", at(0.6)), at(0.6))
    guard.check_manual_command(VIN, "RDU", at(0.7))


async def test_repeat_counts_from_a_slow_confirmation(hass):
    guard = await new_guard(hass)
    record = guard.record_command(VIN, "ROPRECOND", at(0))
    # The vehicle took more than a minute to confirm it
    guard.command_confirmed(record, at(1.1))
    with pytest.raises(AlreadyDone):
        guard.check_manual_command(VIN, "ROPRECOND", at(1.2))
    guard.check_manual_command(VIN, "ROPRECOND", at(2.2))


async def test_locking_and_switching_off_always_go_out(hass):
    guard = await new_guard(hass)
    for command, opposite in (("RDL", "RDU"), ("REOFF", "REON"), ("ROHVACOFF", "ROHVACON")):
        guard.command_confirmed(guard.record_command(VIN, command, at(0)))
        # Even right after the vehicle confirmed it: the state shown may be out
        # of date, and the vehicle must not stay unlocked or running
        guard.check_manual_command(VIN, command, at(0.1))
        guard.command_confirmed(guard.record_command(VIN, opposite, at(0.2)))
        guard.check_manual_command(VIN, command, at(0.3))


async def test_hourly_limit_stops_a_loop(hass):
    guard = await new_guard(hass)
    for i in range(MANUAL_MAX_PER_HOUR):
        guard.check_manual_command(VIN, "HBLF", at(i))
        guard.record_command(VIN, "HBLF", at(i))
    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "RDU", at(MANUAL_MAX_PER_HOUR))
    assert err.value.key == "hourly_limit"
    guard.check_manual_command(VIN, "RDU", at(61))


async def test_hourly_limit_lets_locking_through(hass):
    guard = await new_guard(hass)
    guard.record_command(VIN, "RDU", at(0))
    for i in range(1, MANUAL_MAX_PER_HOUR):
        guard.record_command(VIN, "HBLF", at(i))

    # Locking after the unlock goes out despite the limit
    guard.check_manual_command(VIN, "RDL", at(MANUAL_MAX_PER_HOUR))
    guard.record_command(VIN, "RDL", at(MANUAL_MAX_PER_HOUR))

    # Locking again goes out too: the other commands used up the hour, not
    # the lock, and they must never hold a lock back
    guard.check_manual_command(VIN, "RDL", at(MANUAL_MAX_PER_HOUR + 1))


async def test_locking_again_and_again_is_a_loop(hass):
    guard = await new_guard(hass)
    for i in range(MANUAL_MAX_PER_HOUR):
        guard.record_command(VIN, "RDL", at(i))

    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "RDL", at(MANUAL_MAX_PER_HOUR))
    assert err.value.key == "hourly_limit"


async def test_lock_retried_all_night_still_goes_out(hass):
    # An automation retried the lock every 5 minutes without reception
    guard = await new_guard(hass)
    for i in range(MANUAL_MAX_PER_DAY):
        guard.check_manual_command(VIN, "RDL", at(i * 5))
        guard.record_command(VIN, "RDL", at(i * 5))

    # The next morning the hourly limit is long over, and so is the block
    guard.check_manual_command(VIN, "RDL", at(12 * 60))


async def test_securing_commands_taking_turns_are_a_loop(hass):
    guard = await new_guard(hass)
    for i in range(MANUAL_MAX_PER_HOUR):
        command = ("ROPRECOND_OFF", "ROHVACOFF")[i % 2]
        guard.check_manual_command(VIN, command, at(i))
        guard.record_command(VIN, command, at(i))

    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "ROPRECOND_OFF", at(MANUAL_MAX_PER_HOUR))
    assert err.value.key == "hourly_limit"
    # Switching off after switching on still goes out
    guard.record_command(VIN, "ROPRECOND", at(MANUAL_MAX_PER_HOUR))
    guard.check_manual_command(VIN, "ROPRECOND_OFF", at(MANUAL_MAX_PER_HOUR + 0.5))


async def test_lock_repeated_today_is_not_held_by_other_commands(hass):
    # Locked this morning; later an automation sent 100 other commands
    guard = await new_guard(hass)
    guard.record_command(VIN, "RDL", at(0))
    for i in range(MANUAL_MAX_PER_DAY):
        guard.record_command(VIN, "HBLF", at(600 + i * 5))

    guard.check_manual_command(VIN, "RDL", at(600 + MANUAL_MAX_PER_DAY * 5))


async def test_clock_set_back_does_not_hold_commands_back(hass):
    guard = await new_guard(hass)
    # Recorded while the clock was three hours ahead
    guard.command_confirmed(guard.record_command(VIN, "RDU", at(180)))
    guard.record_command(VIN, "DEEPREFRESH", at(180))
    for _ in range(MANUAL_MAX_PER_HOUR):
        guard.record_command(VIN, "HBLF", at(180))

    guard.check_manual_command(VIN, "RDU", at(0))
    guard.check_manual_command(VIN, "DEEPREFRESH", at(0))
    guard.check_manual_command(VIN, "HBLF", at(0))


async def test_history_survives_a_restart(hass, hass_storage):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    assert begin(guard, 3).decision is Decision.ALLOWED
    guard.auto_refresh_done(VIN, 50.0, at(3))
    await guard.async_flush()

    saved = hass_storage[STORAGE_KEY]["data"]["vehicles"][VIN]
    assert saved["wakes"][0]["command"] == "DEEPREFRESH"
    assert saved["session"]["auto_refreshes"] == 1

    restarted = await new_guard(hass)
    decision = restarted.begin_auto_refresh(VIN, "DEEPREFRESH", INTERVAL, at(10))
    assert decision.decision is Decision.WAIT
    assert restarted.session(VIN).auto_refreshes == 1
    assert restarted.session(VIN).soc_at_last_refresh == 50.0


async def test_record_reaches_storage_without_flush(hass, hass_storage):
    guard = await new_guard(hass)
    guard.record_command(VIN, "RDL", at(0))
    # Saved with no delay, the loop only has to get to it
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["vehicles"][VIN]["wakes"][0]["command"] == "RDL"


async def test_clock_jumps(hass):
    guard = await new_guard(hass)
    guard.charging_started(VIN, at(0))
    guard.record_command(VIN, "RDL", at(60))

    # The clock went back an hour: the command counts as just now
    decision = begin(guard, 0)
    assert decision.decision is Decision.WAIT
    assert decision.wait == AUTO_REFRESH_MIN_SPACING

    # A timestamp years ahead is dropped instead of blocking for good
    guard.record_command(VIN, "RDL", T0.replace(year=2038))
    guard.record_command(VIN, "RDL", at(-100))
    assert begin(guard, 80).decision is Decision.ALLOWED


async def test_a_long_day_keeps_its_history(hass):
    guard = await new_guard(hass)
    guard._vehicle(VIN, at(0)).wakes.extend(
        WakeRecord(at(i), "DEEPREFRESH", True) for i in range(AUTO_REFRESH_MAX_PER_DAY)
    )
    # A loop of locks, as many as the hourly limit lets through in a day
    for i in range(600):
        guard.record_command(VIN, "RDL", at(100 + i))

    assert guard.auto_refreshes_last_day(VIN, at(800)) == AUTO_REFRESH_MAX_PER_DAY


async def test_clock_behind_keeps_the_history(hass, hass_storage):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        guard.record_command(VIN, "RDL", at(0))

        # Booted with a clock days behind: ignored, but neither lost
        early = at(-3 * 24 * 60)
        assert guard.last_wake(VIN, early) is None
        await guard.async_flush()
        assert hass_storage[STORAGE_KEY]["data"]["vehicles"][VIN]["wakes"]
        # and back once the clock is corrected
        assert guard.last_wake(VIN, at(1)) == at(0)

        # A clock behind them for a whole day means they were dated wrong
        assert guard.last_wake(VIN, early) is None
        clock[0] += HISTORY_WINDOW.total_seconds()
        assert guard.last_wake(VIN, early) is None
        assert guard.last_wake(VIN, at(1)) is None


async def test_clock_ahead_does_not_hold_refreshes_back_once_corrected(hass):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        guard._started = clock[0] - STARTUP_GRACE.total_seconds()
        # Refreshed while the clock was a day and a half ahead
        guard.charging_started(VIN, at(-60))
        guard.record_command(VIN, "DEEPREFRESH", at(36 * 60))
        assert begin(guard, 0).decision is Decision.ALLOWED
        guard.auto_refresh_done(VIN, 50.0, at(0))

        # Half a day later the clock comes within a day of it, moving towards
        # it rather than jumping past: it was dated wrong
        clock[0] += 13 * 3600
        assert begin(guard, 13 * 60).decision is Decision.ALLOWED


async def test_wakes_set_aside_stay_so_over_a_restart(hass, hass_storage):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        # Refreshed while the clock was a day and a half ahead
        guard.charging_started(VIN, at(-60))
        guard.record_command(VIN, "DEEPREFRESH", at(36 * 60))
        assert guard.auto_refreshes_last_day(VIN, at(0)) == 0

        # Restarted once the clock came within a day of it, with nothing
        # saved after it was set aside but the delayed save of that
        clock[0] += 13 * 3600
        assert guard.auto_refreshes_last_day(VIN, at(13 * 60)) == 0
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        restarted = await new_guard(hass)
        restarted._started = clock[0] - STARTUP_GRACE.total_seconds()
        assert restarted.auto_refreshes_last_day(VIN, at(13 * 60)) == 0
        assert restarted.last_wake(VIN, at(13 * 60)) is None
        assert begin(restarted, 13 * 60).decision is Decision.ALLOWED

        # The day they may stay aside ran on from before the restart
        clock[0] += 11 * 3600
        assert not restarted._vehicle(VIN, at(24 * 60)).ahead
        # Only the refresh made after the restart counts
        assert restarted.auto_refreshes_last_day(VIN, at(30 * 60)) == 1


async def test_wakes_set_aside_count_the_downtime_of_a_restart(hass, hass_storage):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        guard.charging_started(VIN, at(-60))
        # Refreshed while the clock was a day and a half ahead
        guard.record_command(VIN, "DEEPREFRESH", at(36 * 60))
        assert guard.auto_refreshes_last_day(VIN, at(0)) == 0
        clock[0] += 13 * 3600
        assert guard.auto_refreshes_last_day(VIN, at(13 * 60)) == 0
        await guard.async_flush()

        # Down for 20 hours, and the host rebooted: the monotonic clock
        # started anew
        clock[0] = 100.0
        with patch(
            "custom_components.uconnect.wake_guard._utcnow", lambda: at(33 * 60)
        ):
            restarted = await new_guard(hass)
        restarted._started = clock[0] - STARTUP_GRACE.total_seconds()
        assert restarted.auto_refreshes_last_day(VIN, at(33 * 60)) == 0

        # The clock reaching it does not make it a wake-up of just now
        clock[0] += 4 * 3600
        assert restarted.last_wake(VIN, at(37 * 60)) is None
        assert restarted.auto_refreshes_last_day(VIN, at(37 * 60)) == 0


async def test_wakes_set_aside_by_a_corrected_clock_stay_so_over_a_restart(
    hass, hass_storage
):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        # Refreshed while the clock was a day and a half ahead, and saved
        guard.charging_started(VIN, at(-60))
        guard.record_command(VIN, "DEEPREFRESH", at(0))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        # The clock is corrected, nothing else is saved before a restart
        clock[0] += 60
        assert guard.last_wake(VIN, at(-36 * 60)) is None
        clock[0] += 13 * 3600
        assert guard.last_wake(VIN, at(-23 * 60)) is None
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        restarted = await new_guard(hass)
        assert restarted.last_wake(VIN, at(-23 * 60)) is None


async def test_clock_running_wrong_keeps_the_wakes_set_aside(hass):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        guard._vehicle(VIN, at(0)).wakes.extend(
            WakeRecord(at(-i * 60), "DEEPREFRESH", True) for i in (0, 4)
        )

        # Started with the clock a day and a half behind, and running on wrong
        # for hours, which brings it within a day of them
        for hour in range(13):
            assert guard.auto_refreshes_last_day(VIN, at((hour - 36) * 60)) == 0
            clock[0] += 3600

        # Corrected at last: they are from today and count
        assert guard.auto_refreshes_last_day(VIN, at(0)) == 2


async def test_clock_jumping_to_within_a_day_drops_the_wakes_set_aside(hass):
    clock = [1000.0]
    with patch("custom_components.uconnect.wake_guard.monotonic", lambda: clock[0]):
        guard = await new_guard(hass)
        guard._vehicle(VIN, at(36 * 60)).wakes.append(
            WakeRecord(at(36 * 60), "DEEPREFRESH", True)
        )
        assert guard.auto_refreshes_last_day(VIN, at(0)) == 0

        # The clock jumps half a day, not past it: it was dated wrong
        clock[0] += 60
        assert guard.auto_refreshes_last_day(VIN, at(13 * 60)) == 0
        assert not guard._vehicle(VIN, at(13 * 60)).ahead
        assert guard.auto_refreshes_last_day(VIN, at(37 * 60)) == 0


async def test_damaged_storage_is_ignored(hass, hass_storage):
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "migrations": ["a", 3],
            "vehicles": {
                VIN: {
                    "wakes": [{"at": "not a date"}, "junk", {"at": at(0).isoformat()}],
                    "session": {"auto_refreshes": -3, "without_progress": "x"},
                },
                "junk": "x",
                "ZFAEFAC39MX000002": {"wakes": 5},
            },
        },
    }
    guard = await new_guard(hass)
    assert guard.last_wake(VIN, at(1)) == at(0)
    assert guard.session(VIN).auto_refreshes == 0
    assert guard.session(VIN).without_progress == 0
    assert guard.is_migrated("a") and not guard.is_migrated("3")


async def test_legacy_history_is_imported_once(hass):
    guard = await new_guard(hass)
    guard.import_legacy(VIN, [at(-20), at(-5)], True)
    assert guard.last_auto_refresh(VIN, at(0)) == at(-5)
    assert guard.session(VIN).auto_refreshes == 2
    # The interval counts from the last imported one
    decision = begin(guard, 10)
    assert decision.decision is Decision.WAIT
    assert decision.wait == timedelta(minutes=15)

    guard.import_legacy(VIN, [at(-1)], True)
    assert guard.last_auto_refresh(VIN, at(0)) == at(-5)
