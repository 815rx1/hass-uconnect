"""A week in the life of a charging EV, minute by minute.

The vehicle is driven twice a day, plugged in at home in the evening and
charges on a schedule at night. On top of that: restarts, a crash, a cloud
outage, a changed option, solar surplus charging and a lost upload. Every
command the vehicle receives is checked against what it was really doing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import pytest

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.uconnect.const import DOMAIN
from custom_components.uconnect.wake_guard import DATA_WAKE_GUARD, WakeGuard

START = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SENSOR = "sensor.fiat_500e_extrapolated_battery"


class Car:
    """What the vehicle really does, and what it last uploaded."""

    def __init__(self, vehicle) -> None:
        self.cloud = vehicle  # what the integration reads
        self.soc = 60.0
        self.charging = False
        self.plugged = False
        self.ignition = False
        self.lose_next_stop_upload = False
        self.commands: list[tuple[datetime, str, bool]] = []

    def upload(self) -> None:
        self.cloud.state_of_charge = int(self.soc)
        self.cloud.charging = self.charging
        self.cloud.plugged_in = self.plugged
        self.cloud.ignition_on = self.ignition
        self.cloud.timestamp_ev_info = dt_util.utcnow()

    def command(self, vin, cmd) -> bool:
        self.commands.append((dt_util.utcnow(), cmd.name, self.charging))
        if cmd.name in ("DEEPREFRESH", "VF"):
            self.upload()
        return True

    # --- events -----------------------------------------------------------

    def start_charging(self) -> None:
        if self.plugged and not self.charging and self.soc < 100:
            self.charging = True
            self.upload()

    def stop_charging(self) -> None:
        if self.charging:
            self.charging = False
            if self.lose_next_stop_upload:
                self.lose_next_stop_upload = False
                return
            self.upload()

    def minute(self) -> None:
        if self.charging:
            self.soc = min(self.soc + 0.3, 100.0)
            if self.soc >= 100:
                self.stop_charging()
        elif self.ignition:
            self.soc = max(self.soc - 0.25, 5.0)


async def tick(hass, freezer) -> None:
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def restart(hass, config_entry, snapshot=None) -> None:
    """Restart Home Assistant; with a snapshot, as after a crash."""
    guard = hass.data[DOMAIN][DATA_WAKE_GUARD]
    if snapshot is None:
        await guard.async_flush()
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    if snapshot is not None:
        # The restore cache is only saved every 15 minutes
        mock_restore_cache(hass, [snapshot])
    fresh = WakeGuard(hass)
    await fresh.async_load()
    hass.data[DOMAIN][DATA_WAKE_GUARD] = fresh
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("interval", [30, 0])
async def test_a_week(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle, interval
):
    # The frozen clock makes every step look slow to asyncio
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    freezer.move_to(START)
    car = Car(vehicle)
    car.upload()
    mock_client.command_verify.side_effect = car.command
    outage = {"on": False}

    def refresh():
        if outage["on"]:
            raise Exception("cloud down")

    mock_client.refresh.side_effect = refresh

    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "charging_refresh_interval": interval}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    snapshot = hass.states.get(SENSOR)
    charge_windows: list[list[datetime]] = []

    for minute in range(7 * 24 * 60):
        now = START + timedelta(minutes=minute)
        day = (now - START.replace(hour=0)).days  # night of day N is 01:00 of day N
        hm = (now.hour, now.minute)

        was_charging = car.charging
        # Daily routine
        if hm == (1, 0):
            if day == 6:
                car.lose_next_stop_upload = True
            car.start_charging()
        elif hm == (5, 0):
            car.stop_charging()
        elif hm == (7, 0):
            car.plugged = False
            car.ignition = True
            car.upload()
        elif hm == (8, 0):
            car.ignition = False
            car.upload()
        elif hm == (17, 0):
            car.plugged = False
            car.ignition = True
            car.upload()
        elif hm == (18, 0):
            car.ignition = False
            car.upload()
        elif hm == (18, 5):
            car.plugged = True
            car.upload()

        # Day 5: back home at 10:00, solar surplus switches the wallbox
        if day == 5:
            if hm == (10, 0):
                car.plugged = True
                car.upload()
            elif (11, 0) <= hm < (15, 0) and now.minute % 10 == 0:
                if car.charging:
                    car.stop_charging()
                else:
                    car.start_charging()
            elif hm == (15, 0):
                car.stop_charging()
            elif hm == (16, 55):
                car.plugged = False
                car.upload()

        car.minute()
        if car.charging and not was_charging:
            charge_windows.append([now, None])
        elif was_charging and not car.charging:
            charge_windows[-1][1] = now

        # Day 3: the cloud is down around the start of the charge
        outage["on"] = day == 3 and (0, 50) <= hm < (1, 40)

        await tick(hass, freezer)

        if minute % 15 == 0:
            snapshot = hass.states.get(SENSOR)

        # Day 2: restart right after the charge started
        if day == 2 and hm == (1, 2):
            await restart(hass, config_entry)
        # Day 7: crash during the charge
        if day == 7 and hm == (1, 10):
            await restart(hass, config_entry, snapshot)

    commands = car.commands
    names = {name for _, name, _ in commands}
    assert names <= {"DEEPREFRESH"}, names

    # Never two wake-ups within 5 minutes, never more than 72 in a day
    times = [at for at, _, _ in commands]
    for earlier, later in zip(times, times[1:]):
        assert later - earlier >= timedelta(minutes=5), (earlier, later)
    for at in times:
        assert sum(1 for t in times if at <= t < at + timedelta(days=1)) <= 72

    # Only while really charging. The one exception follows the interval:
    # the refresh that finds out about the charge whose end was never uploaded
    idle = [at for at, _, charging in commands if not charging]
    assert len(idle) == (1 if interval else 0), idle
    assert all((at - START.replace(hour=0)).days == 6 for at in idle)

    # Every charge got its refresh at the start, a few minutes in, the solar
    # charging with its short breaks counting as one. The restart and the
    # outage delayed it.
    previous_end = None
    for start, end in charge_windows:
        new_charge = previous_end is None or start - previous_end >= timedelta(minutes=30)
        previous_end = end
        if not new_charge:
            continue
        first = next((at for at in times if at >= start), None)
        assert first is not None
        late = start.day in (21, 22)
        assert first - start <= timedelta(minutes=60 if late else 10), (start, first)

    if not interval:
        # One per charge, the solar charging with short breaks being one
        per_day: dict[int, int] = {}
        for at in times:
            day = (at - START.replace(hour=0)).days
            per_day[day] = per_day.get(day, 0) + 1
        assert per_day == {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 1, 7: 1}
