"""Tests for the robustness and safety changes in the vendored library."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.uconnect.pyuconnect import client as client_module
from custom_components.uconnect.pyuconnect.api import (
    API,
    LOGIN_BACKOFF_START,
    AuthenticationError,
    LoginBackoffError,
    PinRejectedError,
    _is_token_rejected,
)
from custom_components.uconnect.pyuconnect.command import COMMAND_DOORS_LOCK
from custom_components.uconnect.pyuconnect.brands import FIAT_EU
from custom_components.uconnect.pyuconnect.client import (
    STATUS_BLOCK_MAX_AGE,
    Client,
    Vehicle,
    _merge_status,
    _update_vehicle,
)

from .conftest import VIN, load_fixture
from .test_pyuconnect import logged_in_api, response

LIST_ITEM = {
    "vin": VIN,
    "make": "FIAT",
    "modelDescription": "500e",
    "soldRegion": "EMEA",
    "nickname": "500",
    "services": [
        {"service": "RDL", "vehicleCapable": True, "serviceEnabled": True},
        {"service": "RDU", "vehicleCapable": True, "serviceEnabled": True},
        {"service": "CPPLUS", "vehicleCapable": True, "serviceEnabled": True},
        {"service": "REON", "vehicleCapable": True, "serviceEnabled": False},
    ],
}

REMOTE_STATUS = {
    "timestamp": 1760000300000,
    "doors": {
        "driver": {"status": "LOCKED"},
        "passenger": {"status": "UNLOCKED"},
        "leftRear": {"status": "NA"},
        "rightRear": {"status": "LOCKED"},
    },
    "windows": {"driver": {"status": "CLOSED"}, "passenger": {"status": "NA"}},
    "trunk": {"status": "NA"},
    "liftGate": {"status": "LOCKED"},
    "engine": {"status": "NA"},
    "evRunning": {"status": "OFF"},
    "hvac": {"status": "ON"},
}


def make_client(statuses, remote=None, vehicles=None) -> Client:
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    api = MagicMock()
    api.list_vehicles.return_value = vehicles if vehicles is not None else [LIST_ITEM]
    api.get_vehicle.side_effect = statuses
    api.get_vehicle_location.return_value = {"latitude": 48.1, "longitude": 11.5}
    api.get_vehicle_status.return_value = remote if remote is not None else REMOTE_STATUS
    client.api = api
    return client


# --- Remote status ------------------------------------------------------


def test_na_states_are_unknown_not_unlocked():
    client = make_client([load_fixture("status_500e.json")])
    client.refresh()
    v = client.vehicles[VIN]

    assert v.door_driver_locked is True
    assert v.door_passenger_locked is False
    assert v.door_rear_left_locked is None
    assert v.door_rear_right_locked is True
    assert v.window_passenger_closed is None
    assert v.trunk_locked is None
    assert v.liftgate_locked is True
    assert v.ignition_on is None
    assert v.hvac_on is True


def test_services_are_known_even_if_the_status_fails():
    client = make_client([requests.exceptions.ReadTimeout("slow")])
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.refresh()
    v = client.vehicles[VIN]
    assert v.supported_commands == ["RDL", "RDU"]
    assert "CPPLUS" in v.enabled_services
    assert "REON" not in v.enabled_services


def test_numeric_nickname_stays_text():
    client = make_client([load_fixture("status_500e.json")])
    client.refresh()
    assert client.vehicles[VIN].nickname == "500"


def test_vehicle_gone_from_the_account_is_dropped():
    client = make_client([load_fixture("status_500e.json")] * 3)
    client.refresh()
    assert VIN in client.vehicles

    # An empty list is a hiccup, nothing is dropped
    client.api.list_vehicles.return_value = []
    client.refresh()
    assert VIN in client.vehicles

    other = dict(LIST_ITEM, vin="ZFAOTHERCAR0000001")
    client.api.list_vehicles.return_value = [other]
    client.refresh()
    assert VIN not in client.vehicles


# --- Parsing ------------------------------------------------------------


def vehicle_from(payload: dict) -> Vehicle:
    v = Vehicle(vin=VIN, nickname=None, make="FIAT", model="500e", year=2022, region="EMEA")
    _update_vehicle(v, payload)
    return v


def test_empty_range_is_a_value():
    payload = load_fixture("status_500e.json")
    payload["evInfo"]["battery"]["distanceToEmpty"] = {"value": 0, "unit": "km"}
    v = vehicle_from(payload)
    assert v.distance_to_empty == 0
    assert v.distance_to_empty_unit == "km"


def test_odometer_never_goes_back():
    payload = load_fixture("status_500e.json")
    v = vehicle_from(payload)
    assert v.odometer == 12345

    lower = copy.deepcopy(payload)
    lower["vehicleInfo"]["odometer"]["odometer"]["value"] = "0"
    _update_vehicle(v, lower)
    assert v.odometer == 12345

    higher = copy.deepcopy(payload)
    higher["vehicleInfo"]["odometer"]["odometer"]["value"] = "12400"
    _update_vehicle(v, higher)
    assert v.odometer == 12400

    # In another unit the number may well be lower
    miles = copy.deepcopy(payload)
    miles["vehicleInfo"]["odometer"]["odometer"] = {"value": "7705", "unit": "mi"}
    _update_vehicle(v, miles)
    assert (v.odometer, v.odometer_unit) == (7705, "mi")


def test_bad_payloads_do_not_break_the_refresh():
    payload = load_fixture("status_500e.json")
    payload["vehicleInfo"]["tyrePressure"] = None
    payload["evInfo"]["schedules"] = [{"startTime": "01:00"}, "junk"]
    v = vehicle_from(payload)
    assert v.charge_schedules == []
    assert v.state_of_charge == 72


def test_empty_block_counts_as_missing():
    previous = load_fixture("status_500e.json")
    current = {"vehicleInfo": previous["vehicleInfo"], "evInfo": {}}
    fresh = {}
    merged = _merge_status(previous, current, fresh, now=1000.0)
    assert merged["evInfo"] == previous["evInfo"]


def test_carried_block_expires():
    previous = load_fixture("status_500e.json")
    fresh = {}
    _merge_status(None, previous, fresh, now=0.0)

    partial = {"vehicleInfo": previous["vehicleInfo"]}
    merged = _merge_status(previous, partial, fresh, now=STATUS_BLOCK_MAX_AGE - 1)
    assert "evInfo" in merged

    merged = _merge_status(previous, partial, fresh, now=STATUS_BLOCK_MAX_AGE + 1)
    assert "evInfo" not in merged


# --- Login protection ---------------------------------------------------


def logged_out_api() -> API:
    return API("user@example.com", "pw", "1234", FIAT_EU)


def test_failed_login_backs_off():
    api = logged_out_api()
    api.login = MagicMock(side_effect=Exception("Gigya rate limit 403048"))

    with pytest.raises(Exception, match="unable to login"):
        api._refresh_token_if_needed()
    # Within the backoff no request is made at all
    with pytest.raises(LoginBackoffError):
        api._refresh_token_if_needed()
    assert api.login.call_count == 1

    # Somebody sending a command is not held back
    api.bypass_login_backoff = True
    with pytest.raises(Exception, match="unable to login"):
        api._refresh_token_if_needed()
    assert api.login.call_count == 2


def test_backoff_grows_and_resets():
    api = logged_out_api()
    api.login = MagicMock(side_effect=Exception("down"))
    clock = [1000.0]

    with patch("custom_components.uconnect.pyuconnect.api.time.monotonic", lambda: clock[0]):
        for failures in range(1, 4):
            with pytest.raises(Exception, match="unable to login"):
                api._refresh_token_if_needed()
            wait = LOGIN_BACKOFF_START * 2 ** (failures - 1)
            assert api._next_login_attempt == pytest.approx(clock[0] + wait)
            clock[0] += wait + 1

        def ok():
            api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)

        api.login = MagicMock(side_effect=ok)
        api._refresh_token_if_needed()
        assert api._login_failures == 0


def test_rejected_credentials_are_not_tried_again():
    api = logged_out_api()
    api.login = MagicMock(side_effect=AuthenticationError("account login failed: 403042"))

    with pytest.raises(AuthenticationError):
        api._refresh_token_if_needed()
    api.bypass_login_backoff = True
    with pytest.raises(AuthenticationError):
        api._refresh_token_if_needed()
    assert api.login.call_count == 1


def test_rejected_token_relogin_is_rate_limited():
    api = logged_out_api()
    api.uid = "uid1"
    api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    logins = []

    def login():
        logins.append(1)
        api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)

    api.login = login
    rejected = response(401, {"message": "Unauthorized"})
    with patch.object(api.sess, "request", return_value=rejected):
        for _ in range(3):
            with pytest.raises(requests.HTTPError):
                api.list_vehicles()

    # One fresh login, not one per request
    assert len(logins) == 1


def test_login_logs_no_secrets(caplog):
    api = logged_out_api()
    secrets = {
        "login": "LOGIN-TOKEN-SECRET",
        "jwt": "ID-TOKEN-SECRET",
        "token": "COGNITO-TOKEN-SECRET",
        "access": "ACCESS-KEY-SECRET",
        "session": "SESSION-TOKEN-SECRET",
    }

    def handler(self, method, url, **kwargs):
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        if url.endswith("/accounts.login"):
            return response(
                body={
                    "statusCode": 200,
                    "UID": "uid1",
                    "sessionInfo": {"login_token": secrets["login"]},
                    "profile": {"email": "user@example.com"},
                }
            )
        if url.endswith("/accounts.getJWT"):
            return response(body={"statusCode": 200, "id_token": secrets["jwt"]})
        if url == FIAT_EU.token_url:
            return response(body={"Token": secrets["token"], "IdentityId": "eu-west-1:x"})
        return response(
            body={
                "Credentials": {
                    "AccessKeyId": secrets["access"],
                    "SecretKey": "SECRET-KEY-SECRET",
                    "SessionToken": secrets["session"],
                    "Expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
                }
            }
        )

    caplog.set_level(logging.DEBUG)
    with patch.object(requests.Session, "request", autospec=True, side_effect=handler):
        api.login()

    for value in (*secrets.values(), "SECRET-KEY-SECRET", "user@example.com"):
        assert value not in caplog.text


def test_failed_login_message_has_no_account_data():
    api = logged_out_api()

    def handler(self, method, url, **kwargs):
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        return response(
            body={
                "statusCode": 403,
                "errorCode": 403042,
                "errorMessage": "Invalid LoginID",
                "errorDetails": "user@example.com",
            }
        )

    with patch.object(requests.Session, "request", autospec=True, side_effect=handler):
        with pytest.raises(AuthenticationError) as err:
            api.login()
    assert "403042" in str(err.value)
    assert "user@example.com" not in str(err.value)


# --- Command status poll ------------------------------------------------


def test_running_command_is_not_a_failure():
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    client.api.get_remote_operation_status.side_effect = requests.HTTPError("404")
    client.api.get_vehicle_notifications.side_effect = [
        {"notifications": {"items": [{"correlationId": "c", "notification": {"data": {"status": "pending"}}}]}},
        {"notifications": {"items": [{"correlationId": "c", "notification": {"data": {"status": "success"}}}]}},
    ]
    with patch.object(client_module, "sleep"):
        assert client._poll_correlation_id(VIN, "c") is True


def test_poll_ends_on_the_monotonic_clock():
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    client.api.get_remote_operation_status.return_value = {"status": "pending"}
    client.api.get_vehicle_notifications.return_value = {"notifications": {"items": []}}
    ticks = iter([0.0, 10.0, 70.0])

    with patch.object(client_module, "sleep"), patch.object(
        client_module, "monotonic", lambda: next(ticks)
    ):
        with pytest.raises(Exception, match="timed out"):
            client._poll_correlation_id(VIN, "c")
    assert client.api.get_remote_operation_status.call_count == 1


# --- Charge schedules ---------------------------------------------------


def schedule_client(status: dict) -> Client:
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    client.api.get_vehicle.side_effect = lambda vin: copy.deepcopy(status)
    return client


def test_plan_reads_the_current_schedules():
    status = load_fixture("status_500e.json")
    status["evInfo"]["schedules"][1]["futureKey"] = {"kept": True}
    client = schedule_client(status)

    planned = client.plan_charge_schedule_emea(VIN, 1, {"enable": True, "start_time": "02:00"})

    assert len(planned) == 3
    assert planned[1]["enableScheduleType"] is True
    assert planned[1]["startTime"] == "02:00"
    assert planned[1]["scheduleType"] == "CHARGE"
    # Keys the library does not know are kept, the other slots unchanged
    assert planned[1]["futureKey"] == {"kept": True}
    assert planned[0] == status["evInfo"]["schedules"][0]
    assert planned[2] == status["evInfo"]["schedules"][2]


def test_plan_without_change_is_none():
    client = schedule_client(load_fixture("status_500e.json"))
    assert client.plan_charge_schedule_emea(VIN, 1, {"enable": False}) is None
    assert client.plan_charge_schedule_emea(VIN, 0, {"start_time": "01:00"}) is None
    assert (
        client.plan_charge_schedule_emea(
            VIN, 0, {"days": ["monday", "tuesday", "wednesday", "thursday", "friday"]}
        )
        is None
    )


def test_plan_refuses_without_current_schedules():
    status = load_fixture("status_500e.json")
    del status["evInfo"]["schedules"]
    client = schedule_client(status)
    with pytest.raises(ValueError, match="could not be read"):
        client.plan_charge_schedule_emea(VIN, 0, {"enable": True})

    client = schedule_client(load_fixture("status_500e.json"))
    with pytest.raises(ValueError, match="does not exist"):
        client.plan_charge_schedule_emea(VIN, 5, {"enable": True})


def test_disabling_keeps_the_schedule_type():
    client = schedule_client(load_fixture("status_500e.json"))
    planned = client.plan_charge_schedule_emea(VIN, 0, {"enable": False})
    assert planned[0]["enableScheduleType"] is False
    assert planned[0]["scheduleType"] == "CHARGE"


def test_sent_schedules_are_shown_right_away():
    client = make_client([load_fixture("status_500e.json")])
    client.refresh()
    client.api.set_charge_schedule_emea.return_value = "corr"
    planned = schedule_client(load_fixture("status_500e.json")).plan_charge_schedule_emea(
        VIN, 2, {"enable": True}
    )

    with patch.object(client, "_poll_correlation_id", return_value=True):
        assert client.send_charge_schedules_emea(VIN, planned) is True
    assert client.vehicles[VIN].charge_schedules[2].enable is True
    sent = client.api.set_charge_schedule_emea.call_args.args[1]
    assert json.dumps(sent) == json.dumps(planned)


# --- Secrets stay out of errors and logs --------------------------------


def login_handler(fail_call: str, failure):
    """Route the login to canned answers, failing the given Gigya call."""

    def handler(self, method, url, **kw):
        if url.endswith("/" + fail_call):
            if isinstance(failure, Exception):
                raise failure
            return response(failure, {"error": "x"})
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        if url.endswith("/accounts.login"):
            return response(
                body={"statusCode": 200, "UID": "uid1", "sessionInfo": {"login_token": "LTOKEN"}}
            )
        raise AssertionError(url)

    return handler


@pytest.mark.parametrize(
    ("call", "failure"),
    [
        ("accounts.login", 503),
        ("accounts.login", 403),
        ("accounts.login", requests.exceptions.ConnectTimeout(
            "HTTPSConnectionPool: /accounts.login?loginID=user%40example.com&password=Sup3rSecret"
        )),
        ("accounts.getJWT", 500),
    ],
)
def test_login_errors_carry_no_secrets(call, failure):
    api = API("user@example.com", "Sup3rSecret", "1234", FIAT_EU)
    with patch.object(
        requests.Session, "request", autospec=True, side_effect=login_handler(call, failure)
    ):
        with pytest.raises(Exception) as err:
            api._refresh_token_if_needed()

    seen = []
    error = err.value
    while error is not None:
        seen.append(str(error))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    text = " ".join(seen)
    assert call in text
    for secret in ("Sup3rSecret", "user@example.com", "user%40example.com", "LTOKEN"):
        assert secret not in text


def test_http_errors_leave_out_account_and_vehicle():
    api = logged_in_api()
    failed = response(500, {"debugMsg": "boom"})
    failed.url = f"https://channels.example.invalid/v1/accounts/uid1/vehicles/{VIN}/remote/status"
    with patch.object(api.sess, "request", return_value=failed):
        with pytest.raises(requests.HTTPError) as err:
            api.get_vehicle_status(VIN)
    assert "uid1" not in str(err.value) and VIN not in str(err.value)
    assert "HTTP 500" in str(err.value)
    # Callers still see the status
    assert err.value.response.status_code == 500


def test_debug_log_leaves_out_vin_position_and_account(caplog):
    api = logged_in_api()
    body = {"vin": VIN, "uid": "uid1", "latitude": 48.137, "longitude": "11.575", "soc": 80}
    with patch.object(api.sess, "request", return_value=response(200, body)):
        with caplog.at_level(logging.DEBUG, logger="custom_components.uconnect.pyuconnect.api"):
            api.get_vehicle_location(VIN)
    text = caplog.text
    assert "get_vehicle_location" in text and "80" in text
    for secret in (VIN, "uid1", "48.137", "11.575"):
        assert secret not in text


def test_signing_library_does_not_log_secrets():
    assert logging.getLogger("requests_auth_aws_sigv4").getEffectiveLevel() >= logging.WARNING


# --- The PIN ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [(403, {"name": "INVALID_PIN"}), (423, {"name": "PIN_LOCKED"}), (401, {"message": "no"})],
)
def test_rejected_pin_is_not_sent_again(status, body):
    api = logged_in_api()
    api._last_rejected_relogin = 0.0
    with patch(
        "custom_components.uconnect.pyuconnect.api.time.monotonic", return_value=100.0
    ), patch.object(api.sess, "request", return_value=response(status, body)) as req:
        with pytest.raises(PinRejectedError):
            api.command(VIN, COMMAND_DOORS_LOCK)
        sent = req.call_count
        # Nothing goes out any more, neither a command nor the PIN
        with pytest.raises(PinRejectedError):
            api.command(VIN, COMMAND_DOORS_LOCK)
        assert req.call_count == sent

    # A new PIN is tried again
    api.set_pin("5678")
    answers = [
        response(200, {"token": "pin-1", "expiry": 300000}),
        response(200, {"responseStatus": "pending", "correlationId": "a"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers):
        assert api.command(VIN, COMMAND_DOORS_LOCK) == "a"


def test_pin_that_cannot_be_right_is_not_sent():
    api = logged_in_api()
    api.set_pin("12 34")
    with patch.object(api.sess, "request") as req:
        with pytest.raises(PinRejectedError):
            api.command(VIN, COMMAND_DOORS_LOCK)
    req.assert_not_called()


def test_command_queued_is_never_sent_twice():
    # An answer that queued the command is no rejected token, whatever it says
    queued = response(
        200, {"responseStatus": "pending", "correlationId": "a", "note": "expired_token"}
    )
    assert not _is_token_rejected(queued)
    assert _is_token_rejected(response(200, {"name": "EXPIRED_TOKEN"}))


# --- The login backoff outlives the client -------------------------------


def test_backoff_is_kept_for_a_new_client():
    api = logged_out_api()
    api.login = MagicMock(side_effect=Exception("down"))
    with pytest.raises(Exception, match="unable to login"):
        api._refresh_token_if_needed()

    # Home Assistant retrying the setup creates a new client
    retry = logged_out_api()
    retry.login = MagicMock()
    with pytest.raises(LoginBackoffError):
        retry._refresh_token_if_needed()
    retry.login.assert_not_called()

    # A new password is tried at once
    fixed = API("user@example.com", "new-pw", "1234", FIAT_EU)
    fixed.login = MagicMock(side_effect=Exception("still down"))
    with pytest.raises(Exception, match="unable to login"):
        fixed._refresh_token_if_needed()
    fixed.login.assert_called_once()
