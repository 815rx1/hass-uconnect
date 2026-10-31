import requests
import uuid
import json
import base64
import hashlib
import logging
import re
import threading
import time
import http.client as http_client
from typing import Any
from urllib.parse import urlsplit

from dataclasses import dataclass
from dataclasses_json import dataclass_json

from datetime import datetime, timedelta, timezone
from requests_auth_aws_sigv4 import AWSSigV4

from .command import Command
from .brands import Brand

_LOGGER = logging.getLogger(__name__)

# The signing library logs the session token and the signed request bodies,
# which carry the PIN, at debug level. A global debug log would otherwise put
# them into bug reports.
logging.getLogger("requests_auth_aws_sigv4").setLevel(logging.WARNING)

# Connect and read timeout in seconds applied to every request
DEFAULT_TIMEOUT = (10, 30)

# Gigya error codes of accounts.login that mean the credentials were rejected
AUTH_ERROR_CODES = {401030, 403042}


# A cached PIN authentication token is renewed this many seconds before the
# lifetime the backend gives it runs out
PIN_TOKEN_MARGIN = 15

# After a failed login the next one waits this long, doubling with every
# further failure up to the maximum. Logging in again on every poll while the
# login service is failing or rate limiting can get the account locked.
LOGIN_BACKOFF_START = 60
LOGIN_BACKOFF_MAX = 30 * 60

# Credentials the backend rejects before they expire are renewed at most this
# often. An endpoint answering 401 for good would otherwise cost a full login
# on every request.
TOKEN_REJECTED_RELOGIN_INTERVAL = 10 * 60


class AuthenticationError(Exception):
    """The login was rejected because of invalid credentials"""


class LoginBackoffError(Exception):
    """No login was attempted because the last ones failed shortly before"""


class PinRejectedError(Exception):
    """The backend rejected the PIN.

    It is not sent again until it is changed: every further attempt with a
    wrong PIN brings the account closer to a PIN lock, which also blocks the
    commands of the official app.
    """


class UconnectHTTPError(requests.exceptions.HTTPError):
    """An HTTP error whose message leaves out the account and the vehicle"""


class PinAuthError(UconnectHTTPError):
    """The PIN authentication failed without rejecting the PIN.

    Nothing reached the vehicle, and the reason is not the vehicle: a rate
    limit, the service being down, or credentials it did not take.
    """


# Markers in an answer that mean the AWS credentials were refused, as opposed
# to the request itself
_CREDENTIAL_MARKERS = (
    "security token",
    "signature expired",
    "expired_token",
    "token has expired",
)

_ACCOUNT_IN_PATH = re.compile(r"(/accounts/)[^/?#]+")
_VEHICLE_IN_PATH = re.compile(r"(/vehicles/)[^/?#]+")
_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_COORDINATE = re.compile(
    r'("(?:latitude|longitude|lat|lon|lng)"\s*:\s*)"?-?[0-9.]+"?', re.IGNORECASE
)


def _redacted_path(url: str | None) -> str:
    path = urlsplit(url or "").path
    path = _ACCOUNT_IN_PATH.sub(r"\1*", path)
    return _VEHICLE_IN_PATH.sub(r"\1*", path)


def _raise_for_status(
    r: requests.Response, error: type[UconnectHTTPError] = UconnectHTTPError
) -> None:
    """Raises for an HTTP error status like requests does, but without the URL.

    requests quotes the full URL, which holds the account ID and the VIN, and
    that message ends up in the log and in errors shown in the frontend.
    """

    if r.status_code < 400:
        return

    method = r.request.method if r.request is not None else None
    where = " ".join(x for x in (method, _redacted_path(r.url)) if x)
    raise error(
        f"HTTP {r.status_code} {r.reason or ''}".rstrip() + (f" on {where}" if where else ""),
        response=r,
    )


def _masked_vin(vin: str | None) -> str:
    """The last characters of a VIN, enough to tell vehicles apart in a log"""

    return f"…{vin[-4:]}" if vin else "?"


def _redact(text: str, *secrets: str | None) -> str:
    """Removes account details, VINs and coordinates from a logged answer"""

    for secret in secrets:
        if secret:
            text = text.replace(secret, "*")
    text = _VIN.sub("<vin>", text)
    return _COORDINATE.sub(r"\1<redacted>", text)


@dataclass
class _LoginBackoff:
    failures: int = 0
    next_attempt: float = 0.0


# The login backoff is kept per account and password beyond one API object:
# Home Assistant creates a new client on every setup retry, which would
# otherwise start the backoff from scratch and log in every few minutes. A new
# password starts without backoff.
_LOGIN_BACKOFFS: dict[str, _LoginBackoff] = {}


def _login_backoff(login_url: str, email: str | None, password: str | None) -> _LoginBackoff:
    key = hashlib.sha256(
        f"{login_url}\n{(email or '').casefold()}\n{password or ''}".encode()
    ).hexdigest()
    return _LOGIN_BACKOFFS.setdefault(key, _LoginBackoff())


# A rejected PIN is kept per account beyond one API object too, for the same
# reason: a reload creates a new client, and sending the same wrong PIN again
# brings the PIN lock closer, which also blocks the official app. Holds the
# rejected PIN and whether the backend reported the PIN locked.
_PIN_REJECTIONS: dict[str, tuple[str, bool]] = {}


def _account_key(login_url: str, email: str | None) -> str:
    return hashlib.sha256(f"{login_url}\n{(email or '').casefold()}".encode()).hexdigest()


def forget_pin_rejection(login_url: str, email: str | None) -> None:
    """Lets the PIN of an account be sent again when no API object is at hand"""

    _PIN_REJECTIONS.pop(_account_key(login_url, email), None)


def _login_error(r: dict) -> str:
    """Describes a failed login response without anything account related."""

    if not isinstance(r, dict):
        return "unexpected response"
    return (
        f"errorCode {r.get('errorCode')}, statusCode {r.get('statusCode')}: "
        f"{r.get('errorMessage') or r.get('statusReason') or 'no message'}"
    )


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

    if r.status_code == 200:
        # Only the error envelope, never an answer that queued a command,
        # which would then be sent a second time
        try:
            body = r.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and str(body.get("name", "")).upper() == "EXPIRED_TOKEN"
            and "correlationId" not in body
        )

    return _names_credentials(r)


def _names_credentials(r: requests.Response) -> bool:
    """Tells whether an error answer names the AWS credentials as the reason"""

    if len(r.content) > 4096:
        return False
    text = r.text.lower()
    return any(marker in text for marker in _CREDENTIAL_MARKERS)


def _lists_vehicles(r: requests.Response) -> bool:
    """Tells whether an answer is the vehicle list, which proves the backend
    took the credentials. Its error envelope, or a maintenance page, can come
    with a 200 too."""

    if r.status_code != 200:
        return False
    try:
        body = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "vehicles" in body


def _pin_rejected(r: requests.Response) -> bool:
    """Tells whether the answer of the PIN authentication rejects the PIN.

    The backend answers a wrong PIN with 403 INVALID_PIN, some accounts with
    401, and a locked PIN with 423. A 401 that names the credentials is about
    those, and was already retried with a fresh login; a bare 401 was checked
    against the credentials before it gets here, see API._pin_auth.
    """

    if r.status_code == 423:
        return True
    text = r.text.lower() if len(r.content) <= 4096 else ""
    if r.status_code == 403:
        return "invalid_pin" in text
    if r.status_code == 401:
        return not _names_credentials(r)
    return False


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

        # PIN authentication token and the monotonic time it is valid until
        self._pin_token: tuple[str, float] | None = None
        # A rejected PIN is held in _PIN_REJECTIONS only, see PinRejectedError,
        # so releasing it reaches every API object of the account at once
        self._account = _account_key(brand.login_url, email)
        # Commands, per vehicle, the backend refused with 403 while their
        # fallback went out: they go out as the fallback right away
        self._use_fallback: set[tuple[str, str]] = set()

        # Login protection, see LOGIN_BACKOFF_START. The credentials, once
        # rejected, are not tried again: a new API object gets the new ones.
        self._backoff = _login_backoff(brand.login_url, email, password)
        self._credentials_rejected: str | None = None
        self._last_rejected_relogin: float | None = None
        # Set for calls made on request: a person locking the car waits for
        # nothing, only background polling is held back by the backoff
        self.bypass_login_backoff = False

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

    def _rejection(self) -> tuple[str, bool] | None:
        """The rejection of the PIN set now, with whether it is locked"""
        rejection = _PIN_REJECTIONS.get(self._account)
        return rejection if rejection is not None and rejection[0] == self.pin else None

    @property
    def pin_rejected(self) -> bool:
        """Whether the PIN set now was rejected, so it is not sent again"""
        return self._rejection() is not None

    @property
    def pin_locked(self) -> bool:
        """Whether the backend reported the PIN set now as locked"""
        rejection = self._rejection()
        return rejection is not None and rejection[1]

    def _reject_pin(self, locked: bool) -> None:
        _PIN_REJECTIONS[self._account] = (self.pin, locked)

    def clear_pin_rejection(self) -> None:
        """Lets the PIN be sent again, e.g. once it was unlocked in the app"""
        self._pin_token = None
        _PIN_REJECTIONS.pop(self._account, None)

    def pin_rejection_fingerprint(self, salt: str) -> str:
        """Identifies the PIN set now without revealing it, to keep a
        rejection over a restart."""
        return hashlib.sha256(f"{salt}\n{self.pin}".encode()).hexdigest()[:16]

    def restore_pin_rejection(self, fingerprint: Any, locked: Any, salt: str) -> None:
        """Takes back a rejection of the PIN set now from before a restart"""
        if (
            self.pin
            and isinstance(fingerprint, str)
            and fingerprint == self.pin_rejection_fingerprint(salt)
        ):
            self._reject_pin(locked is True)

    @property
    def _login_failures(self) -> int:
        return self._backoff.failures

    @property
    def _next_login_attempt(self) -> float:
        return self._backoff.next_attempt

    def set_pin(self, pin: str):
        if pin != self.pin:
            self.pin = pin
            self.clear_pin_rejection()
        self.pin = pin

    def _log_response(self, what: str, r: requests.Response) -> None:
        """Logs an answer at debug level without account details or position"""

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "%s: HTTP %s %s",
                what,
                r.status_code,
                _redact(r.text[:4000], self.uid, self.email),
            )

    def _gigya(self, method: str, call: str, params: dict) -> dict:
        """Calls the login service, keeping secrets out of any error raised.

        The login sends the password and the session tokens as URL
        parameters, and requests quotes the URL in its errors, which reach
        the log and the frontend. The answer carries the session and the
        profile, so only its outcome is logged.
        """

        try:
            r = self.sess.request(
                method=method, url=f"{self.brand.login_url}/{call}", params=params
            )
        except requests.exceptions.RequestException as err:
            raise Exception(f"{call}: {type(err).__name__}") from None

        if r.status_code >= 400:
            raise Exception(f"{call}: HTTP {r.status_code}")

        try:
            body = r.json()
        except ValueError:
            raise Exception(f"{call}: the answer is not JSON") from None

        if not isinstance(body, dict):
            raise Exception(f"{call}: unexpected answer")

        _LOGGER.debug("Login: %s: statusCode %s", call, body.get("statusCode"))
        return body

    def login(self):
        """Logs into the Uconnect and caches the auth tokens"""

        r = self._gigya(
            "GET", "accounts.webSdkBootstrap", {"apiKey": self.brand.login_api_key}
        )

        if r.get("statusCode") != 200:
            raise Exception(f"bootstrap failed: {_login_error(r)}")

        r = self._gigya(
            "POST",
            "accounts.login",
            self._with_default_params(
                {
                    "loginID": self.email,
                    "password": self.password,
                    "sessionExpiration": 300,
                    "include": "profile,data,emails,subscriptions,preferences",
                }
            ),
        )

        if r.get("statusCode") != 200:
            if r.get("errorCode") in AUTH_ERROR_CODES:
                raise AuthenticationError(f"account login failed: {_login_error(r)}")
            raise Exception(f"account login failed: {_login_error(r)}")

        self.uid = r["UID"]
        login_token = r["sessionInfo"]["login_token"]

        r = self._gigya(
            "POST",
            "accounts.getJWT",
            self._with_default_params(
                {
                    "login_token": login_token,
                    "fields": "profile.firstName,profile.lastName,profile.email,country,locale,data.disclaimerCodeGSDP",
                }
            ),
        )

        if r.get("statusCode") != 200 or not r.get("id_token"):
            raise Exception(f"unable to obtain JWT: {_login_error(r)}")

        r = self.sess.request(
            method="POST",
            url=self.brand.token_url,
            headers=self._default_aws_headers(self.brand.api.key)
            | {"content-type": "application/json"},
            json={"gigya_token": r["id_token"]},
        )

        _raise_for_status(r)
        r = r.json()
        _LOGGER.debug("Login: obtain token: keys %s", sorted(r) if isinstance(r, dict) else None)

        token = r.get("Token", None)
        identity_id = r.get("IdentityId", None)
        if token is None or identity_id is None:
            raise Exception(
                f"unable to obtain identity & token: keys {sorted(r) if isinstance(r, dict) else r}"
            )

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

        _raise_for_status(r)
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

            # Trying rejected credentials again only brings the account
            # closer to a lockout
            if self._credentials_rejected is not None:
                raise AuthenticationError(
                    f"the credentials were rejected: {self._credentials_rejected}"
                )

            backoff = self._backoff
            wait = backoff.next_attempt - time.monotonic()
            if wait > 0 and not self.bypass_login_backoff:
                raise LoginBackoffError(
                    f"login paused for another {int(wait) + 1} s after "
                    f"{backoff.failures} failed attempts"
                )

            try:
                self.login()
            except AuthenticationError as e:
                self._credentials_rejected = str(e)
                raise
            except Exception as e:
                backoff.failures += 1
                delay = min(
                    LOGIN_BACKOFF_START * 2 ** (backoff.failures - 1),
                    LOGIN_BACKOFF_MAX,
                )
                backoff.next_attempt = time.monotonic() + delay
                raise Exception(f"unable to login: {e}") from e

            backoff.failures = 0
            backoff.next_attempt = 0.0

    def _invalidate_token(self):
        """Forces a fresh login on the next request"""

        with self._login_lock:
            self.expire_time = None

    def _aws_request(
        self,
        method: str,
        url: str,
        key: str | None = None,
        relogin: str = "any",
        **kwargs,
    ):
        """Sends a SigV4 signed request, logging in again once if rejected.

        The credentials are only renewed shortly before they expire, so ones
        the backend stopped accepting earlier would otherwise keep every
        request failing until then, which can be most of an hour.

        relogin "marked" logs in again only when the answer names the
        credentials, which the PIN authentication needs: a bare 401 can be a
        wrong PIN there, and resending it would cost a second attempt towards
        a PIN lock. "never" sends the request once as it is.
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

            # A request carrying the PIN token that fails may have failed over
            # the token, so the next one asks for a fresh token
            if r.status_code >= 400 and "pinAuth" in (kwargs.get("json") or {}):
                self._pin_token = None

            if (
                attempt == 0
                and not self.dev_mode
                and relogin != "never"
                and _is_token_rejected(r)
                and (relogin == "any" or r.status_code != 401 or _names_credentials(r))
            ):
                now = time.monotonic()
                last = self._last_rejected_relogin
                if last is not None and now - last < TOKEN_REJECTED_RELOGIN_INTERVAL:
                    _LOGGER.debug(
                        "Credentials rejected (HTTP %s) again shortly after a "
                        "fresh login, not logging in again yet",
                        r.status_code,
                    )
                    return r

                _LOGGER.info(
                    "Credentials rejected (HTTP %s), logging in again", r.status_code
                )
                self._last_rejected_relogin = now
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

        _raise_for_status(r)
        self._log_response("list_vehicles", r)
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

                _raise_for_status(r)
                self._log_response(f"get_vehicle {api_version}", r)
                return r.json()

            except requests.exceptions.HTTPError as err:
                last_error = err
                status_code = err.response.status_code if err.response is not None else None

                if status_code not in (400, 404, 502):
                    raise

                if index < len(api_versions) - 1:
                    _LOGGER.warning(
                        "Vehicle %s status endpoint %s failed with HTTP %s; trying next status endpoint",
                        _masked_vin(vin),
                        api_version,
                        status_code,
                    )
                else:
                    _LOGGER.warning(
                        "Vehicle %s status endpoint %s failed with HTTP %s; no detailed status endpoint remains",
                        _masked_vin(vin),
                        api_version,
                        status_code,
                    )

        _LOGGER.warning(
            "Vehicle %s does not support v3/v4 detailed status; loading partial vehicle data: %s",
            _masked_vin(vin),
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

        _raise_for_status(r)
        self._log_response("get_vehicle_status", r)
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

        _raise_for_status(r)
        self._log_response("get_vehicle_location", r)
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

        _raise_for_status(r)
        self._log_response("get_vehicle_health_report", r)
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

        _raise_for_status(r)
        self._log_response("get_maintenance_history", r)
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

        _raise_for_status(r)
        self._log_response("get_eco_coaching_last_trip", r)
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

        _raise_for_status(r)
        self._log_response("get_eco_coaching_trips", r)
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

        _raise_for_status(r)
        self._log_response("get_vehicle_image", r)
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

        _raise_for_status(r)
        self._log_response("get_vehicle_notifications", r)
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

        _raise_for_status(r)
        self._log_response("get_stolen_vehicle_status", r)
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

        _raise_for_status(r)
        self._log_response("get_vehicle_subscription", r)
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

        _raise_for_status(r)
        self._log_response("set_vehicle_nickname", r)
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

        _raise_for_status(r)
        self._log_response("update_location", r)
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

        _raise_for_status(r)
        self._log_response("get_remote_operation_status", r)
        r = r.json()

        return r

    def _pin_auth(self) -> str:
        """Returns a PIN authentication token, reusing one until it expires.

        The official apps authenticate once and send several commands with the
        token, so asking for a new one before every command only doubles the
        requests.
        """

        cached = self._pin_token
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]

        if self.pin_rejected:
            raise PinRejectedError(
                "the PIN is locked, unlock it in the official app"
                if self.pin_locked
                else "the PIN was rejected and is not sent again until it is changed"
            )
        if not self.pin or not str(self.pin).isdigit():
            raise PinRejectedError("the PIN must consist of digits only")

        data = {
            "pin": base64.b64encode(self.pin.encode()).decode(encoding="utf-8"),
        }

        try:
            self._refresh_token_if_needed()
            r = self._post_pin(data)
            if r.status_code == 401 and not _names_credentials(r):
                r = self._recheck_bare_401(data, r)
        except PinAuthError:
            raise
        except requests.exceptions.RequestException as err:
            # A timeout or a dropped connection: the vehicle was not reached.
            # Only the kind of error, the text of one names the account.
            reason = str(err) if isinstance(err, UconnectHTTPError) else type(err).__name__
            raise PinAuthError(f"PIN authentication failed: {reason}") from err

        if _pin_rejected(r):
            self._reject_pin(r.status_code == 423)
            raise PinRejectedError(
                "the PIN is locked, unlock it in the official app"
                if r.status_code == 423
                else f"the PIN was rejected (HTTP {r.status_code})"
            )

        _raise_for_status(r, PinAuthError)
        try:
            answer = r.json()
        except ValueError as err:
            raise PinAuthError("PIN authentication failed: no JSON answer", response=r) from err
        _LOGGER.debug(
            "pin auth: token %s",
            "received" if isinstance(answer, dict) and answer.get("token") else "missing",
        )

        if not isinstance(answer, dict) or "token" not in answer:
            raise PinAuthError(
                "PIN authentication failed: no token found: keys "
                f"{sorted(answer) if isinstance(answer, dict) else type(answer).__name__}",
                response=r,
            )

        # The expiry is the token's lifetime in milliseconds. Without one the
        # token is used once, as before.
        expiry = answer.get("expiry")
        if (
            isinstance(expiry, (int, float))
            and not isinstance(expiry, bool)
            and expiry / 1000 > PIN_TOKEN_MARGIN
        ):
            self._pin_token = (
                answer["token"],
                time.monotonic() + expiry / 1000 - PIN_TOKEN_MARGIN,
            )

        return answer["token"]

    def _post_pin(self, data: dict) -> requests.Response:
        return self._aws_request(
            method="POST",
            url=self.brand.auth.url
            + f"/v1/accounts/{self.uid}/ignite/pin/authenticate",
            key=self.brand.auth.token,
            relogin="marked",
            json=data,
        )

    def _recheck_bare_401(self, data: dict, r: requests.Response) -> requests.Response:
        """Tells a wrong PIN from credentials the backend stopped taking.

        Some accounts answer a wrong PIN with a bare 401, which is also what
        refused credentials can look like. Sending the PIN again with fresh
        credentials costs a second attempt towards a PIN lock, so the
        credentials are checked with a read first: answered with the vehicle
        list, they are taken, the PIN was wrong and the answer stands.
        Refused, they are renewed and the PIN is sent once more; a bare 401 to
        that is the PIN once the fresh credentials read the list. Any other
        answer to a read proves neither.
        """

        probe = self._read_vehicle_list()
        if _lists_vehicles(probe):
            return r

        now = time.monotonic()
        last = self._last_rejected_relogin
        if not _is_token_rejected(probe) or (
            last is not None and now - last < TOKEN_REJECTED_RELOGIN_INTERVAL
        ):
            # Not proven either way, e.g. the service failing: neither marked
            # as a wrong PIN nor sent again
            _raise_for_status(r, PinAuthError)

        _LOGGER.info("Credentials rejected at the PIN authentication, logging in again")
        self._last_rejected_relogin = now
        self._invalidate_token()
        r = self._post_pin(data)
        if (
            r.status_code == 401
            and not _names_credentials(r)
            and not _lists_vehicles(self._read_vehicle_list())
        ):
            # Everything refused, also with fresh credentials: an outage, not
            # proof of a wrong PIN
            _raise_for_status(r, PinAuthError)
        return r

    def _read_vehicle_list(self) -> requests.Response:
        """Reads the vehicle list once, as it is, to see whether the backend
        takes the credentials"""

        return self._aws_request(
            method="GET",
            url=self.brand.api.url + f"/v4/accounts/{self.uid}/vehicles",
            params={"stage": "ALL", "sdp": "ALL", "brand": self.brand.brand_code},
            relogin="never",
        )

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

        _raise_for_status(r)
        self._log_response("command execute", r)
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
        fallback = getattr(cmd, "fallback", None)

        # A fallback the backend took after a 403 goes out first from then on,
        # saving a request each time. Refused in any way, the command itself
        # is tried again the next time: the 403 can depend on the vehicle
        # state, a 500e refuses DEEPREFRESH only while not plugged in and
        # takes it while charging, but never DEEPREFRESH2.
        if fallback is not None and (vin, cmd.name) in self._use_fallback:
            try:
                return self._command_with_pin_auth(vin, fallback, pin_auth)
            except requests.exceptions.HTTPError:
                self._use_fallback.discard((vin, cmd.name))
                raise

        try:
            return self._command_with_pin_auth(vin, cmd, pin_auth)
        except requests.exceptions.HTTPError as err:
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
                # Remembered only once the fallback went out: refused as well,
                # it says nothing about which variant the vehicle takes
                result = self._command_with_pin_auth(vin, fallback, pin_auth)
                self._use_fallback.add((vin, cmd.name))
                return result

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

        _raise_for_status(r)
        self._log_response("get_charge_schedules", r)
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

        _raise_for_status(r)
        self._log_response("set_charge_schedule", r)
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

        _raise_for_status(r)
        self._log_response("set_charge_schedule_emea", r)
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

        _raise_for_status(r)
        self._log_response("set charging level", r)
        r = r.json()

        if "correlationId" not in r:
            error = r.get("debugMsg", "unknown error")
            raise Exception(f"set charging level failed: {error} ({r})")

        return r["correlationId"]
