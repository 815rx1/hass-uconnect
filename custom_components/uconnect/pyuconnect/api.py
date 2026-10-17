import requests
import uuid
import json
import base64
import logging
import threading
import http.client as http_client

from dataclasses import dataclass
from dataclasses_json import dataclass_json

from datetime import datetime, timedelta, timezone
from requests_auth_aws_sigv4 import AWSSigV4

from .command import Command
from .brands import Brand

_LOGGER = logging.getLogger(__name__)

# Connect and read timeout in seconds applied to every request
DEFAULT_TIMEOUT = (10, 30)

# Gigya error codes of accounts.login that mean the credentials were rejected
AUTH_ERROR_CODES = {401030, 403042}


class AuthenticationError(Exception):
    """The login was rejected because of invalid credentials"""


def _parse_expiration(value) -> datetime:
    """Parses the credential expiry, which the JSON API sends in epoch seconds"""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_token_rejected(r: requests.Response) -> bool:
    """Tells whether a response means the AWS credentials are no longer valid.

    A plain 403 also comes back for commands the vehicle refuses, so it only
    counts when the body names the credentials.
    """

    if r.status_code == 401:
        return True

    if r.status_code not in (200, 403) or len(r.content) > 4096:
        return False

    text = r.text.lower()
    if r.status_code == 200:
        return "expired_token" in text

    return any(
        marker in text
        for marker in ("security token", "signature expired", "expired_token", "token has expired")
    )


class _TimeoutSession(requests.Session):
    """Session that applies a default timeout to requests made without one.

    Without a timeout a server that stops answering blocks the caller
    forever, which leaves a polling application stuck until it is restarted.
    """

    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().request(method, url, **kwargs)


@dataclass_json
@dataclass
class ChargingLevel:
    name: str


CHARGING_LEVEL_ONE = ChargingLevel("LEVEL_ONE")
CHARGING_LEVEL_TWO = ChargingLevel("LEVEL_TWO")
CHARGING_LEVEL_THREE = ChargingLevel("LEVEL_THREE")
CHARGING_LEVEL_FOUR = ChargingLevel("LEVEL_FOUR")
CHARGING_LEVEL_FIVE = ChargingLevel("LEVEL_FIVE")

CHARGING_LEVELS = [
    CHARGING_LEVEL_ONE,
    CHARGING_LEVEL_TWO,
    CHARGING_LEVEL_THREE,
    CHARGING_LEVEL_FOUR,
    CHARGING_LEVEL_FIVE,
]

CHARGING_LEVELS_BY_NAME = {x.name: x for x in CHARGING_LEVELS}


class API:
    def __init__(
        self,
        email: str,
        password: str,
        pin: str,
        brand: Brand,
        disable_tls_verification: bool = False,
        dev_mode: bool = False,
        trace: bool = False,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    ):
        self.email = email
        self.password = password
        self.pin = pin
        self.brand = brand
        self.dev_mode = dev_mode
        self.timeout = timeout

        self.uid: str | None = None
        self.aws_auth: AWSSigV4 | None = None

        # Guards the token refresh, so that callers using the API from
        # several threads do not log in at the same time
        self._login_lock = threading.Lock()

        self.sess = _TimeoutSession(timeout)
        self.sess.headers.update({"User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )})

        self.expire_time: datetime | None = None

        if disable_tls_verification:
            self.sess.verify = False
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if trace:
            http_client.HTTPConnection.debuglevel = 1
            logging.basicConfig()
            logging.getLogger().setLevel(logging.DEBUG)
            requests_log = logging.getLogger("requests.packages.urllib3")
            requests_log.setLevel(logging.DEBUG)
            requests_log.propagate = True

    def _with_default_params(self, params: dict):
        return params | {
            "targetEnv": "jssdk",
            "loginMode": "standard",
            "sdk": "js_latest",
            "authMode": "cookie",
            "sdkBuild": "12234",
            "format": "json",
            "APIKey": self.brand.login_api_key,
        }

    def _default_aws_headers(self, key: str):
        return {
            "x-clientapp-name": "CWP",
            "x-clientapp-version": "1.0",
            "clientrequestid": uuid.uuid4().hex.upper()[0:16],
            "x-api-key": key,
            "locale": self.brand.locale,
            "x-originator-type": "web",
        }

    def set_debug(self, debug: bool):
        _LOGGER.setLevel(logging.DEBUG if debug else logging.WARNING)

    def set_tls_verification(self, verify: bool):
        self.sess.verify = verify

    def set_pin(self, pin: str):
        self.pin = pin

    def login(self):
        """Logs into the Uconnect and caches the auth tokens"""

        r = self.sess.request(
            method="GET",
            url=self.brand.login_url + "/accounts.webSdkBootstrap",
            params={"apiKey": self.brand.login_api_key},
        )

        r.raise_for_status()
        _LOGGER.debug(f"Login: accounts.webSdkBootstrap: {r.text}")
        r = r.json()

        if r["statusCode"] != 200:
            raise Exception(f"bootstrap failed: {r}")

        r = self.sess.request(
            method="POST",
            url=self.brand.login_url + "/accounts.login",
            params=self._with_default_params(
                {
                    "loginID": self.email,
                    "password": self.password,
                    "sessionExpiration": 300,
                    "include": "profile,data,emails,subscriptions,preferences",
                }
            ),
        )

        r.raise_for_status()
        _LOGGER.debug(f"Login: accounts.login: {r.text}")
        r = r.json()

        if r["statusCode"] != 200:
            if r.get("errorCode") in AUTH_ERROR_CODES:
                raise AuthenticationError(f"account login failed: {r}")
            raise Exception(f"account login failed: {r}")

        self.uid = r["UID"]
        login_token = r["sessionInfo"]["login_token"]

        r = self.sess.request(
            method="POST",
            url=self.brand.login_url + "/accounts.getJWT",
            params=self._with_default_params(
                {
                    "login_token": login_token,
                    "fields": "profile.firstName,profile.lastName,profile.email,country,locale,data.disclaimerCodeGSDP",
                }
            ),
        )

        r.raise_for_status()
        _LOGGER.debug(f"Login: accounts.getJWT: {r.text}")
        r = r.json()

        if r["statusCode"] != 200:
            raise Exception(f"unable to obtain JWT: {r}")

        r = self.sess.request(
            method="POST",
            url=self.brand.token_url,
            headers=self._default_aws_headers(self.brand.api.key)
            | {"content-type": "application/json"},
            json={"gigya_token": r["id_token"]},
        )

        r.raise_for_status()
        _LOGGER.debug(f"Login: obtain token: {r.text}")
        r = r.json()

        token = r.get("Token", None)
        identity_id = r.get("IdentityId", None)
        if token is None or identity_id is None:
            raise Exception(f"unable to obtain identity & token: {r}")

        creds = self._get_cognito_credentials(identity_id, token)

        self.aws_auth = AWSSigV4(
            "execute-api",
            region=self.brand.region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretKey"],
            aws_session_token=creds["SessionToken"],
        )

        self.expire_time = _parse_expiration(creds["Expiration"])

    def _get_cognito_credentials(self, identity_id: str, token: str) -> dict:
        """Exchanges the identity token for temporary AWS credentials.

        This is the single unsigned Cognito call boto3 was used for. Calling
        the JSON endpoint directly drops boto3 and botocore, whose version
        pins have broken installs before, and puts the call under the same
        request timeout as everything else.
        """

        r = self.sess.request(
            method="POST",
            url=f"https://cognito-identity.{self.brand.region}.amazonaws.com/",
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
            },
            data=json.dumps(
                {
                    "IdentityId": identity_id,
                    "Logins": {"cognito-identity.amazonaws.com": token},
                }
            ),
        )

        r.raise_for_status()
        r = r.json()

        creds = r.get("Credentials", None)
        if not creds or not all(
            k in creds for k in ("AccessKeyId", "SecretKey", "SessionToken", "Expiration")
        ):
            raise Exception(f"unable to obtain AWS credentials: {list(r)}")

        return creds

    def _refresh_token_if_needed(self):
        """Checks if token is available and fresh, refreshes it otherwise"""

        if self.dev_mode:
            return

        with self._login_lock:
            if not self._token_expired():
                return

            try:
                self.login()
            except AuthenticationError:
                raise
            except Exception as e:
                raise Exception(f"unable to login: {e}") from e

    def _invalidate_token(self):
        """Forces a fresh login on the next request"""

        with self._login_lock:
            self.expire_time = None

    def _aws_request(self, method: str, url: str, key: str | None = None, **kwargs):
        """Sends a SigV4 signed request, logging in again once if rejected.

        The credentials are only renewed shortly before they expire, so ones
        the backend stopped accepting earlier would otherwise keep every
        request failing until then, which can be most of an hour.
        """

        for attempt in range(2):
            self._refresh_token_if_needed()

            r = self.sess.request(
                method=method,
                url=url,
                headers=self._default_aws_headers(key or self.brand.api.key)
                | {"content-type": "application/json"},
                auth=self.aws_auth,
                **kwargs,
            )

            if attempt == 0 and not self.dev_mode and _is_token_rejected(r):
                _LOGGER.info(
                    "Credentials rejected (HTTP %s), logging in again", r.status_code
                )
                self._invalidate_token()
                continue

            return r

    def _token_expired(self) -> bool:
        return (
            self.expire_time is None
            or datetime.now().astimezone() > self.expire_time - timedelta(minutes=5)
        )

    def list_vehicles(self) -> list[dict]:
        """Loads a list of vehicles with general info"""

        if self.dev_mode:
            with open("test_list.json") as f:
                return json.load(f)["vehicles"]

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url + f"/v4/accounts/{self.uid}/vehicles",
            params={"stage": "ALL", "sdp": "ALL", "brand": self.brand.brand_code},
        )

        r.raise_for_status()
        _LOGGER.debug(f"list_vehicles: {r.text}")
        r = r.json()

        if "vehicles" not in r:
            raise Exception(f"incorrect response: {r}")

        return r["vehicles"]

    def get_vehicle(self, vin: str) -> dict:
        """Gets detailed info about a vehicle with a given VIN.

        Some older vehicles can be returned by the account vehicle list but fail
        newer detailed status endpoints. Try v3, then v4, then allow the vehicle
        to load with partial data so remote/status and location can still
        populate what is available.
        """

        if self.dev_mode:
            with open(f"test_vehicle_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        last_error = None
        api_versions = ("v3", "v4")

        for index, api_version in enumerate(api_versions):
            try:
                r = self._aws_request(
                    method="GET",
                    url=self.brand.api.url
                    + f"/{api_version}/accounts/{self.uid}/vehicles/{vin}/status/",
                )

                r.raise_for_status()
                _LOGGER.debug(f"get_vehicle ({vin}, {api_version}): {r.text}")
                return r.json()

            except requests.exceptions.HTTPError as err:
                last_error = err
                status_code = err.response.status_code if err.response is not None else None

                if status_code not in (400, 404, 502):
                    raise

                if index < len(api_versions) - 1:
                    _LOGGER.warning(
                        "Vehicle %s status endpoint %s failed with HTTP %s; trying next status endpoint",
                        vin,
                        api_version,
                        status_code,
                    )
                else:
                    _LOGGER.warning(
                        "Vehicle %s status endpoint %s failed with HTTP %s; no detailed status endpoint remains",
                        vin,
                        api_version,
                        status_code,
                    )

        _LOGGER.warning(
            "Vehicle %s does not support v3/v4 detailed status; loading partial vehicle data: %s",
            vin,
            last_error,
        )

        return {}

    def get_vehicle_status(self, vin: str) -> dict:
        """Loads another part of status of a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_vehicle_status_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/remote/status",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_status ({vin}): {r.text}")
        r = r.json()

        return r

    def get_vehicle_location(self, vin: str) -> dict:
        """Gets last known location of a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_vehicle_location_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/location/lastknown",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_location ({vin}): {r.text}")
        r = r.json()

        return r

    def get_vehicle_health_report(self, vin: str) -> dict:
        """Gets vehicle health report for a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_vehicle_vhr_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url + f"/v1/accounts/{self.uid}/vehicles/{vin}/vhr/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_health_report ({vin}): {r.text}")
        r = r.json()

        return r

    def get_maintenance_history(self, vin: str) -> dict:
        """Gets maintenance history for a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_maintenance_history_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/maintenance/history/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_maintenance_history ({vin}): {r.text}")
        r = r.json()

        return r

    def get_eco_coaching_last_trip(self, vin: str) -> dict:
        """Gets eco-coaching data for the last trip of a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_eco_coaching_last_trip_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v2/accounts/{self.uid}/vehicles/{vin}/ecocoaching/get-last-trip/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_eco_coaching_last_trip ({vin}): {r.text}")
        r = r.json()

        return r

    def get_eco_coaching_trips(self, vin: str) -> dict:
        """Gets eco-coaching trip list for a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_eco_coaching_trips_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v2/accounts/{self.uid}/vehicles/{vin}/ecocoaching/get-trips/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_eco_coaching_trips ({vin}): {r.text}")
        r = r.json()

        return r

    def get_vehicle_image(self, vin: str, width: int = 600, height: int = 340) -> dict:
        """Gets vehicle image URL for a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_vehicle_image_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        data = {
            "imageURLs": [{"id": vin, "width": width, "height": height, "resp": "png"}]
        }

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url + f"/v4/accounts/{self.uid}/vehicles/{vin}/image/",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_image ({vin}): {r.text}")
        r = r.json()

        return r

    def get_vehicle_notifications(self, vin: str, limit: int | None = 30) -> dict:
        """Loads notifications for a vehicle with a given VIN"""

        self._refresh_token_if_needed()

        url = (
            self.brand.api.url + f"/v1/accounts/{self.uid}/vehicles/{vin}/notifications"
        )

        if limit is not None:
            url += f"?limit={limit}"

        r = self._aws_request(
            method="GET",
            url=url,
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_notifications ({vin}): {r.text}")
        r = r.json()

        return r

    def get_stolen_vehicle_status(self, vin: str) -> dict:
        """Gets stolen vehicle locator (SVLA) status for a vehicle with a given VIN"""

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/svla/status/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_stolen_vehicle_status ({vin}): {r.text}")
        r = r.json()

        return r

    def get_vehicle_subscription(self, vin: str) -> dict:
        """Gets subscription status for a vehicle with a given VIN"""

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/subscription/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_vehicle_subscription ({vin}): {r.text}")
        r = r.json()

        return r

    def set_vehicle_nickname(self, vin: str, nickname: str) -> dict:
        """Sets a nickname for a vehicle with a given VIN"""

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/nickname/",
            json={"nickname": nickname},
        )

        r.raise_for_status()
        _LOGGER.debug(f"set_vehicle_nickname ({vin} {nickname}): {r.text}")
        r = r.json()

        return r

    def update_location(self, vin: str) -> str:
        """Triggers a fresh location update for a vehicle with a given VIN.

        Returns the correlation ID to poll for completion.
        Use get_vehicle_location() afterwards to retrieve the updated location.
        """

        pin_auth = self._pin_auth()

        data = {
            "command": "VF",
            "pinAuth": pin_auth,
        }

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/location/",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"update_location ({vin}): {r.text}")
        r = r.json()

        if "responseStatus" not in r or r["responseStatus"] != "pending":
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"update location failed: {error} ({r})")

        return r["correlationId"]

    def get_remote_operation_status(self, vin: str, correlation_id: str) -> dict:
        """Gets the status of a remote operation by its correlation ID"""

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v1/accounts/{self.uid}/vehicles/{vin}/remote/{correlation_id}/status/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_remote_operation_status ({vin} {correlation_id}): {r.text}")
        r = r.json()

        return r

    def _pin_auth(self) -> str:
        data = {
            "pin": base64.b64encode(self.pin.encode()).decode(encoding="utf-8"),
        }

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="POST",
            url=self.brand.auth.url
            + f"/v1/accounts/{self.uid}/ignite/pin/authenticate",
            key=self.brand.auth.token,
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"pin auth: {r.text}")
        r = r.json()

        if "token" not in r:
            raise Exception(f"authentication failed: no token found: {r}")

        return r["token"]

    def _command_with_pin_auth(self, vin: str, cmd: Command, pin_auth: str):
        data = {
            "command": cmd.name,
            "pinAuth": pin_auth,
        }

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/{cmd.api_version}/accounts/{self.uid}/vehicles/{vin}/{cmd.url}",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"command execute ({vin} {cmd}): {r.text}")
        r = r.json()

        if "responseStatus" not in r or r["responseStatus"] != "pending":
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"command queuing failed: {error} ({r})")

        return r["correlationId"]

    def command(self, vin: str, cmd: Command):
        """Sends given command to the vehicle with a given VIN"""

        if self.dev_mode:
            return

        pin_auth = self._pin_auth()

        try:
            return self._command_with_pin_auth(vin, cmd, pin_auth)
        except requests.exceptions.HTTPError as err:
            fallback = getattr(cmd, "fallback", None)

            if (
                err.response is not None
                and err.response.status_code == 403
                and fallback is not None
            ):
                _LOGGER.warning(
                    "%s command returned 403; retrying with %s",
                    cmd,
                    fallback,
                )
                return self._command_with_pin_auth(vin, fallback, pin_auth)

            raise

    def get_charge_schedules(self, vin: str) -> dict:
        """Gets EV charge schedules for a vehicle with a given VIN"""

        if self.dev_mode:
            with open(f"test_charge_schedules_{vin}.json") as f:
                return json.load(f)

        self._refresh_token_if_needed()

        r = self._aws_request(
            method="GET",
            url=self.brand.api.url
            + f"/v4/accounts/{self.uid}/vehicles/{vin}/ev/schedule/",
        )

        r.raise_for_status()
        _LOGGER.debug(f"get_charge_schedules ({vin}): {r.text}")
        r = r.json()

        return r

    def set_charge_schedule(self, vin: str, schedule: dict):
        """Sets an EV charge schedule on the vehicle with a given VIN"""

        if self.dev_mode:
            return

        pin_auth = self._pin_auth()

        data = schedule | {"pinAuth": pin_auth}

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v4/accounts/{self.uid}/vehicles/{vin}/ev/schedule/",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"set_charge_schedule ({vin}): {r.text}")
        r = r.json()

        if "correlationId" not in r:
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"set charge schedule failed: {error} ({r})")

        return r["correlationId"]

    def set_charge_schedule_emea(self, vin: str, schedules: list[dict]):
        """Sets EV charge schedules on EMEA vehicles.

        Unlike ``set_charge_schedule`` (which POSTs to ``/v4/.../ev/schedule/``
        and fails with HTTP 500 on EMEA cars such as the Fiat New 500e), this
        posts the full slot list to ``/v2/.../ev/schedule/`` wrapped in a
        ``CPPLUS`` command envelope. Confirmed working via live test on an
        EMEA 500e (soldRegion=EMEA, sdp=IGNITE).

        ``schedules`` must contain *all* slots (typically three); the backend
        replaces the entire list on every write.
        """

        pin_auth = self._pin_auth()

        data = {
            "command": "CPPLUS",
            "pinAuth": pin_auth,
            "schedules": schedules,
        }

        r = self._aws_request(
            method="POST",
            url=self.brand.api.url
            + f"/v2/accounts/{self.uid}/vehicles/{vin}/ev/schedule/",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"set_charge_schedule_emea ({vin}): {r.text}")
        r = r.json()

        if "correlationId" not in r:
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"set charge schedule (emea) failed: {error} ({r})")

        return r["correlationId"]

    def set_charging_level(
        self, vin: str, level: ChargingLevel, max_soc: str | None = None
    ):
        """Sets the charging level on the vehicle with a given VIN"""

        if self.dev_mode:
            return

        pin_auth = self._pin_auth()

        data = {
            "preference": level.name,
            "pinAuth": pin_auth,
        }

        if max_soc is not None:
            data["maxSOC"] = max_soc

        r = self._aws_request(
            method="PUT",
            url=self.brand.api.url
            + f"/v2/accounts/{self.uid}/vehicles/{vin}/ev/charge/preference/",
            json=data,
        )

        r.raise_for_status()
        _LOGGER.debug(f"set charging level ({vin} {level.name}): {r.text}")
        r = r.json()

        if "correlationId" not in r:
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"set charging level failed: {error} ({r})")

        return r["correlationId"]
