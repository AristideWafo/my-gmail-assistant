import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from src.ports import UnsubscribeError

REQUEST_TIMEOUT_SECONDS = 10
ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"
HTTPS_PORT = 443


class HttpUnsubscriber:
    """One-click unsubscribe (RFC 8058): a single HTTPS POST, to a public address only."""

    def unsubscribe(self, url: str) -> None:
        host, target = _parse(url)
        address = _public_address(host)
        try:
            status = self._post(host, address, target)
        except (OSError, http.client.HTTPException) as exc:
            # Never the URL: it usually carries the subscriber's token.
            raise UnsubscribeError(f"request to {host} failed: {type(exc).__name__}") from None
        # Redirects are not followed: the target would be an address nobody checked.
        if not 200 <= status < 300:
            raise UnsubscribeError(f"{host} answered HTTP {status}")

    @staticmethod
    def _post(host: str, address: str, target: str) -> int:
        # Connect to the address that was checked, not to the name: resolving again would let
        # the sender's DNS answer with an internal address the second time.
        raw = socket.create_connection((address, HTTPS_PORT), timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        connection = http.client.HTTPSConnection(host, HTTPS_PORT, timeout=REQUEST_TIMEOUT_SECONDS)
        connection.sock = sock
        try:
            connection.request(
                "POST",
                target,
                body=ONE_CLICK_BODY,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return connection.getresponse().status
        finally:
            connection.close()


def _parse(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsubscribeError("unsubscribe link is not a valid URL") from None
    if parts.scheme != "https" or not parts.hostname:
        raise UnsubscribeError("unsubscribe link is not an https URL")
    if parts.username or parts.password or port not in (None, HTTPS_PORT):
        raise UnsubscribeError("unsubscribe link carries credentials or an unusual port")
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return parts.hostname, target


def _public_address(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsubscribeError(f"{host} does not resolve") from None
    addresses = [info[4][0] for info in infos]
    # Every answer must be public: one private record among several is enough to be steered
    # at an internal service.
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsubscribeError(f"{host} resolves to a non-public address")
    return addresses[0]
