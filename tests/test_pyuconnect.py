"""Tests for the vendored py-uconnect library."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.uconnect.pyuconnect.api import (
    API,
    AuthenticationError,
    DEFAULT_TIMEOUT,
    _is_token_rejected,
    _parse_expiration,
)
from custom_components.uconnect.pyuconnect.brands import FIAT_EU
from custom_components.uconnect.pyuconnect.client import Client
from custom_components.uconnect.pyuconnect.command import COMMAND_DOORS_LOCK

from .conftest import VIN, load_fixture


def response(status: int = 200, body=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r.url = "https://example.invalid"
    return r


def fake_login_responses(expiration=None):
    """Route the login requests to canned answers, like the real backend."""
    expiration = expiration or (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        if url.endswith("/accounts.login"):
            return response(
                body={"statusCode": 200, "UID": "uid1", "sessionInfo": {"login_token": "lt"}}
            )
        if url.endswith("/accounts.getJWT"):
            return response(body={"statusCode": 200, "id_token": "jwt"})
        if url == FIAT_EU.token_url:
            return response(body={"Token": "tok", "IdentityId": "eu-west-1:abc"})
        if "cognito-identity" in url:
            return response(
                body={
                    "IdentityId": "eu-west-1:abc",
                    "Credentials": {
                        "AccessKeyId": "AKIA",
                        "SecretKey": "secret",
                        "SessionToken": "session",
                        "Expiration": expiration,
                    },
                }
            )
        raise AssertionError(f"unexpected request {method} {url}")

    return handler, calls


def test_login_uses_cognito_json_api_and_timeouts():
    api = API("user@example.com", "pw", "1234", FIAT_EU)
    handler, calls = fake_login_responses()

    with patch.object(requests.Session, "request", autospec=True) as req:
        req.side_effect = lambda self, method, url, **kw: handler(method, url, **kw)
        api.login()

    assert api.uid == "uid1"
    assert api.aws_auth is not None
    assert api.expire_time > datetime.now(timezone.utc)
    assert not api._token_expired()

    cognito = next(c for c in calls if "cognito-identity" in c[1])
    assert cognito[1] == "https://cognito-identity.eu-west-1.amazonaws.com/"
    assert cognito[2]["headers"]["X-Amz-Target"] == (
        "AWSCognitoIdentityService.GetCredentialsForIdentity"
    )
    assert json.loads(cognito[2]["data"]) == {
        "IdentityId": "eu-west-1:abc",
        "Logins": {"cognito-identity.amazonaws.com": "tok"},
    }
    # Every request gets the default timeout
    assert all(req.call_args_list[i].kwargs["timeout"] == DEFAULT_TIMEOUT for i in range(len(calls)))


def test_login_rejected_credentials_raise_authentication_error():
    api = API("user@example.com", "wrong", "", FIAT_EU)

    def handler(self, method, url, **kw):
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        return response(body={"statusCode": 403, "errorCode": 403042})

    with patch.object(requests.Session, "request", autospec=True, side_effect=handler):
        with pytest.raises(AuthenticationError):
            api._refresh_token_if_needed()


def test_login_other_failures_are_not_auth_errors():
    api = API("user@example.com", "pw", "", FIAT_EU)

    def handler(self, method, url, **kw):
        if url.endswith("/accounts.webSdkBootstrap"):
            return response(body={"statusCode": 200})
        # Rate limited, which must not ask the user for a new password
        return response(body={"statusCode": 403, "errorCode": 403048})

    with patch.object(requests.Session, "request", autospec=True, side_effect=handler):
        with pytest.raises(Exception) as err:
            api._refresh_token_if_needed()
    assert not isinstance(err.value, AuthenticationError)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"message": "Unauthorized"}, True),
        (403, {"message": "The security token included in the request is expired"}, True),
        (403, {"message": "Signature expired: 20261016T000000Z is now earlier than"}, True),
        (200, {"name": "EXPIRED_TOKEN"}, True),
        (403, {"debugMsg": "FORBIDDEN"}, False),
        (403, {"message": "Vehicle not plugged-in"}, False),
        (200, {"vehicles": []}, False),
        (500, {"message": "security token"}, False),
    ],
)
def test_is_token_rejected(status, body, expected):
    assert _is_token_rejected(response(status, body)) is expected


def test_parse_expiration():
    ts = 1760000000.0
    assert _parse_expiration(ts) == datetime.fromtimestamp(ts, tz=timezone.utc)
    assert _parse_expiration("2026-10-17T12:00:00+00:00").tzinfo is not None
    assert _parse_expiration(datetime(2026, 10, 17, 12)).tzinfo is timezone.utc


def test_signed_request_logs_in_again_when_token_rejected():
    api = API("user@example.com", "pw", "1234", FIAT_EU)
    api.uid = "uid1"
    api.aws_auth = None
    api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    logins = []
    api.login = lambda: (
        logins.append(1),
        setattr(api, "expire_time", datetime.now(timezone.utc) + timedelta(hours=1)),
    )

    answers = [
        response(403, {"message": "The security token included in the request is expired"}),
        response(200, {"vehicles": [{"vin": VIN}]}),
    ]
    with patch.object(api.sess, "request", side_effect=answers) as req:
        assert api.list_vehicles() == [{"vin": VIN}]

    assert len(logins) == 1
    assert req.call_count == 2


def test_signed_request_retries_only_once():
    api = API("user@example.com", "pw", "1234", FIAT_EU)
    api.uid = "uid1"
    api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    api.login = lambda: setattr(
        api, "expire_time", datetime.now(timezone.utc) + timedelta(hours=1)
    )

    rejected = response(401, {"message": "Unauthorized"})
    with patch.object(api.sess, "request", return_value=rejected) as req:
        with pytest.raises(requests.HTTPError):
            api.list_vehicles()
    assert req.call_count == 2


def test_refused_command_does_not_relogin():
    api = API("user@example.com", "pw", "1234", FIAT_EU)
    api.uid = "uid1"
    api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    api.login = MagicMock()

    answers = [
        response(200, {"token": "pin-token"}),
        response(403, {"debugMsg": "FORBIDDEN"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers):
        with pytest.raises(requests.HTTPError):
            api.command(VIN, COMMAND_DOORS_LOCK)
    api.login.assert_not_called()


def make_client(status_payloads, location=None):
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    api = MagicMock()
    api.list_vehicles.return_value = [
        {"vin": VIN, "make": "FIAT", "modelDescription": "500e", "soldRegion": "EMEA"}
    ]
    api.get_vehicle.side_effect = status_payloads
    api.get_vehicle_location.return_value = location or {
        "latitude": 48.1,
        "longitude": 11.5,
        "timeStamp": 1760000000000,
    }
    api.get_vehicle_status.return_value = {}
    client.api = api
    return client


def test_refresh_parses_500e_values():
    client = make_client([load_fixture("status_500e.json")])
    client.refresh()
    v = client.vehicles[VIN]

    assert v.state_of_charge == 72
    assert v.trip_a_distance == 1013 and v.trip_a_distance_unit == "km"
    assert v.trip_b_distance == 14
    assert v.battery_soh == 97
    assert v.timestamp_ev_info == datetime.fromtimestamp(1760000100).astimezone()
    assert v.timestamp_vehicle_info == datetime.fromtimestamp(1760000000).astimezone()
    assert v.charging_level_preference == 3
    assert len(v.charge_schedules) == 3
    assert v.wheel_rear_right_pressure_warning is True


def test_refresh_keeps_values_on_empty_status():
    client = make_client([load_fixture("status_500e.json"), {}])
    client.refresh()
    client.refresh()
    v = client.vehicles[VIN]

    assert v.state_of_charge == 72
    assert v.odometer == 12345


def placeholder_status(soc: int, range_km: int) -> dict:
    """The status with the 0 % packet the cloud sends now and then."""
    status = load_fixture("status_500e.json")
    battery = status["evInfo"]["battery"]
    battery["stateOfCharge"] = soc
    battery["distanceToEmpty"] = {"value": str(range_km), "unit": "km"}
    battery["totalRange"] = range_km
    battery["timeToFullyChargeL2"] = 0
    battery["timeToFullyChargeL3"] = 0
    return status


def test_refresh_ignores_zero_charge_next_to_a_range():
    client = make_client(
        [load_fixture("status_500e.json"), placeholder_status(0, 171)]
    )
    client.refresh()
    client.refresh()
    v = client.vehicles[VIN]

    assert v.state_of_charge == 72
    assert v.time_to_fully_charge_l2 == 205
    # The rest of the status is taken as it is
    assert v.distance_to_empty == 171


def test_refresh_without_earlier_charge_leaves_it_unknown():
    client = make_client([placeholder_status(0, 171)])
    client.refresh()

    assert client.vehicles[VIN].state_of_charge is None


def remote_status(state: str) -> dict:
    return {
        "doors": {
            door: {"status": state}
            for door in ("driver", "passenger", "leftRear", "rightRear")
        },
        "windows": {"driver": {"status": "CLOSED"}, "passenger": {"status": "CLOSED"}},
        "trunk": {"status": state},
        "timestamp": 1760000000000,
    }


def test_placeholder_keeps_the_lock_states():
    client = make_client(
        [load_fixture("status_500e.json"), placeholder_status(0, 55)]
    )
    client.api.get_vehicle_status.side_effect = [
        remote_status("LOCKED"),
        remote_status("UNLOCKED"),
    ]
    client.refresh()
    client.refresh()
    v = client.vehicles[VIN]

    assert v.door_driver_locked is True
    assert v.door_rear_right_locked is True
    assert v.trunk_locked is True
    assert client.placeholders[VIN]["range"] == 55
    assert client.placeholders[VIN]["uploaded"] == datetime.fromtimestamp(
        1760000100
    ).astimezone()


def test_lock_states_are_taken_again_after_a_placeholder():
    client = make_client(
        [placeholder_status(0, 55), load_fixture("status_500e.json")]
    )
    client.api.get_vehicle_status.side_effect = [
        remote_status("LOCKED"),
        remote_status("UNLOCKED"),
    ]
    client.refresh()
    assert client.vehicles[VIN].door_driver_locked is None

    client.refresh()
    v = client.vehicles[VIN]
    assert v.door_driver_locked is False
    assert v.trunk_locked is False


def test_refresh_takes_zero_charge_without_range():
    client = make_client(
        [load_fixture("status_500e.json"), placeholder_status(0, 0)]
    )
    client.refresh()
    client.refresh()

    assert client.vehicles[VIN].state_of_charge == 0


def test_refresh_fills_missing_block_from_last_status():
    full = load_fixture("status_500e.json")
    partial = {"vehicleInfo": dict(full["vehicleInfo"]), "timestamp": 1760000900000}
    partial["vehicleInfo"]["odometer"] = {"odometer": {"value": "12400", "unit": "km"}}

    client = make_client([full, partial])
    client.refresh()
    client.refresh()
    v = client.vehicles[VIN]

    # New value from the partial answer, EV values carried over
    assert v.odometer == 12400
    assert v.state_of_charge == 72


def test_refresh_keeps_position_without_coordinates():
    client = make_client([load_fixture("status_500e.json")] * 2)
    client.refresh()
    client.api.get_vehicle_location.return_value = {"timeStamp": 1760000500000}
    client.refresh()

    assert client.vehicles[VIN].location.latitude == 48.1


def test_poll_backs_off_and_uses_notifications_sparingly():
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    statuses = [{"status": "pending"}] * 5 + [{"status": "SUCCESS"}]
    client.api.get_remote_operation_status.side_effect = statuses
    client.api.get_vehicle_notifications.return_value = {"notifications": {"items": []}}

    sleeps = []
    with patch("custom_components.uconnect.pyuconnect.client.sleep", side_effect=sleeps.append):
        assert client._poll_correlation_id(VIN, "corr") is True

    assert sleeps == [2, 3, 4, 5, 5, 5]
    # Only every third attempt checks the notifications while status works
    assert client.api.get_vehicle_notifications.call_count == 1


def test_poll_falls_back_to_notifications_when_status_fails():
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    client.api.get_remote_operation_status.side_effect = requests.HTTPError("404")
    client.api.get_vehicle_notifications.return_value = {
        "notifications": {
            "items": [
                {"correlationId": "corr", "notification": {"data": {"status": "success"}}}
            ]
        }
    }

    with patch("custom_components.uconnect.pyuconnect.client.sleep"):
        assert client._poll_correlation_id(VIN, "corr") is True


def test_poll_survives_request_timeouts():
    client = Client("user@example.com", "pw", "1234", FIAT_EU)
    client.api = MagicMock()
    client.api.get_remote_operation_status.side_effect = [
        requests.exceptions.ReadTimeout("slow"),
        {"status": "success"},
    ]
    client.api.get_vehicle_notifications.side_effect = requests.exceptions.ReadTimeout("slow")

    with patch("custom_components.uconnect.pyuconnect.client.sleep"):
        assert client._poll_correlation_id(VIN, "corr") is True


def logged_in_api() -> API:
    api = API("user@example.com", "pw", "1234", FIAT_EU)
    api.uid = "uid1"
    api.expire_time = datetime.now(timezone.utc) + timedelta(hours=1)
    return api


def test_pin_token_is_reused_until_it_expires():
    api = logged_in_api()
    answers = [
        response(200, {"token": "pin-1", "expiry": 300000}),
        response(200, {"responseStatus": "pending", "correlationId": "a"}),
        response(200, {"responseStatus": "pending", "correlationId": "b"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers) as req:
        assert api.command(VIN, COMMAND_DOORS_LOCK) == "a"
        assert api.command(VIN, COMMAND_DOORS_LOCK) == "b"

    # One PIN authentication for two commands, both sent with its token
    assert req.call_count == 3
    assert [c.kwargs["json"].get("pinAuth") for c in req.call_args_list[1:]] == [
        "pin-1",
        "pin-1",
    ]

    # Once expired a new one is fetched
    api._pin_token = ("pin-1", 0)
    answers = [
        response(200, {"token": "pin-2", "expiry": 300000}),
        response(200, {"responseStatus": "pending", "correlationId": "c"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers) as req:
        api.command(VIN, COMMAND_DOORS_LOCK)
    assert req.call_args_list[1].kwargs["json"]["pinAuth"] == "pin-2"


def test_pin_token_without_expiry_is_not_cached():
    api = logged_in_api()
    answers = [
        response(200, {"token": "pin-1"}),
        response(200, {"responseStatus": "pending", "correlationId": "a"}),
        response(200, {"token": "pin-2"}),
        response(200, {"responseStatus": "pending", "correlationId": "b"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers) as req:
        api.command(VIN, COMMAND_DOORS_LOCK)
        api.command(VIN, COMMAND_DOORS_LOCK)
    assert req.call_count == 4


def test_failed_command_drops_pin_token():
    api = logged_in_api()
    answers = [
        response(200, {"token": "pin-1", "expiry": 300000}),
        response(403, {"debugMsg": "FORBIDDEN"}),
    ]
    with patch.object(api.sess, "request", side_effect=answers):
        with pytest.raises(requests.HTTPError):
            api.command(VIN, COMMAND_DOORS_LOCK)
    assert api._pin_token is None


def test_changed_pin_drops_pin_token():
    api = logged_in_api()
    api._pin_token = ("pin-1", float("inf"))
    api.set_pin("1234")
    assert api._pin_token is not None
    api.set_pin("9999")
    assert api._pin_token is None
