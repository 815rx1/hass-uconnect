"""Tests for the per-vehicle wake-up history and its limits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.uconnect.wake_guard import (
    AUTO_REFRESH_MAX_FAILURES,
    AUTO_REFRESH_MAX_PER_DAY,
    AUTO_REFRESH_MAX_WITHOUT_PROGRESS,
    AUTO_REFRESH_MIN_SPACING,
    MANUAL_MAX_PER_HOUR,
    MANUAL_REFRESH_COOLDOWN,
    MANUAL_REFRESH_MAX_PER_DAY,
    SESSION_MERGE_WINDOW,
    STARTUP_GRACE,
    STORAGE_KEY,
    Blocked,
    Decision,
    WakeGuard,
)

VIN = "ZFAEFAC39MX000001"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
INTERVAL = timedelta(minutes=30)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def new_guard(hass, started: datetime | None = None) -> WakeGuard:
    guard = WakeGuard(hass)
    await guard.async_load()
    # Started well before the moments the tests use, unless a test is about
    # the start itself
    guard._started = started or T0 - timedelta(days=1)
    return guard


def begin(guard: WakeGuard, minute: float, interval=INTERVAL):
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
    guard = await new_guard(hass, started=at(0))
    guard.charging_started(VIN, at(0))

    decision = begin(guard, 3)
    assert decision.decision is Decision.WAIT
    assert decision.wait == STARTUP_GRACE - timedelta(minutes=3)
    assert begin(guard, 15).decision is Decision.ALLOWED


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
    record = guard.record_command(VIN, "RDL", at(0))

    # Not confirmed yet, a retry is fine
    guard.check_manual_command(VIN, "RDL", at(0.5))

    guard.command_confirmed(record)
    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "RDL", at(0.5))
    assert err.value.key == "repeated_command"
    # A different command, or the same one a minute later, goes out
    guard.check_manual_command(VIN, "RDU", at(0.5))
    guard.check_manual_command(VIN, "RDL", at(1.1))


async def test_hourly_limit_stops_a_loop(hass):
    guard = await new_guard(hass)
    for i in range(MANUAL_MAX_PER_HOUR):
        guard.check_manual_command(VIN, "HBLF", at(i))
        guard.record_command(VIN, "HBLF", at(i))
    with pytest.raises(Blocked) as err:
        guard.check_manual_command(VIN, "RDL", at(MANUAL_MAX_PER_HOUR))
    assert err.value.key == "hourly_limit"
    guard.check_manual_command(VIN, "RDL", at(61))


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
