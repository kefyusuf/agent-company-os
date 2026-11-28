"""M5.5 T-PR2: hardened external HTTP transport seam.

NOT a production integration. In this stage the only endpoint this class may
ever reach is the host-configured allowlist, and the sole consumer is the
localhost fake-provider contract harness inside the test suite. No real
provider, no credentials in code, no non-loopback traffic exists.

The seam is a typed protocol (``ExternalAdapter``); both this transport and
the ``ScriptedExternalAdapter`` satisfy it, checked by mypy in CI. The ledger
layer stays adapter-agnostic: it never learns which adapter it holds, never
sees credentials and never persists raw provider payloads (G-T7).

Boundary rules, all fail-closed BEFORE any network activity:
- endpoint allowlist (G-T6, owner decision #4): host-configured exact origins;
  unknown actions or off-list origins are denied pre-network
- TLS policy (G-T6, owner decision #2): ``https`` everywhere except loopback
  ``http://127.0.0.1``/``http://localhost`` — the documented fixture exception
- credential boundary (G-T3, owner decision #3): the host supplies a resolver
  callable out-of-band; a missing credential for an allowlisted endpoint is
  denied pre-call, never a silent unauthenticated dispatch. Secret VALUES are
  never recorded anywhere — only the fact that one was used.
- failure mapping (G-T5, owner decision #1): definitive 4xx client rejections
  map to ``failed``; 408/429, 5xx, timeouts and connection errors map to
  ``unknown``. Nothing is ever auto-retried.
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol, runtime_checkable

LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
DENY_4XX = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 418, 422})


def _default_resolver(hostname: str) -> list[str]:
    """Stdlib resolver; CI tests inject a stub instead of touching real DNS."""
    infos = socket.getaddrinfo(hostname, None)
    return sorted({str(info[4][0]) for info in infos})


def _is_denied_address(address_text: str) -> bool:
    """T-PR3 Q3 deny-list for non-loopback endpoints: private, loopback,
    link-local (incl. cloud metadata), reserved, multicast and unspecified
    addresses fail closed."""
    address = ipaddress.ip_address(address_text)
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection pinned to a pre-validated address (T-PR3 Q3)."""

    def __init__(self, host: str, *args, pinned_ip: str | None = None, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        if self._pinned_ip is None:
            super().connect()
            return
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated address.

    TLS SNI and certificate verification keep using the ORIGINAL hostname
    (self.host) — the pin changes where we connect, never whom we verify.
    """

    def __init__(self, host: str, *args, pinned_ip: str | None = None, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        if self._pinned_ip is None:
            super().connect()
            return
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             self.timeout)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler whose connections target the pre-validated address."""

    def __init__(self, pinned_ip: str | None):
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(
            functools.partial(_PinnedHTTPConnection, pinned_ip=self._pinned_ip), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler whose connections target the pre-validated address."""

    def __init__(self, pinned_ip: str | None):
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            functools.partial(_PinnedHTTPSConnection, pinned_ip=self._pinned_ip), req)


class _DenyRedirects(urllib.request.HTTPRedirectHandler):
    """SSRF hardening (owner-approved disposition, 2026-11-27): a redirect
    could point a validated request outside the allowlist, so redirects are
    never followed. The surfaced outcome maps to 'unknown' — the effect state
    is genuinely undetermined once a provider starts redirecting."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     'TRANSPORT_REDIRECT_DENIED', headers, fp)


@runtime_checkable
class ExternalAdapter(Protocol):
    """The typed adapter seam the ledger layer depends on (M5.5 T-PR2)."""

    def transport_key(self, attempt_id: str) -> str: ...

    def dispatch(self, attempt: dict) -> dict: ...

    def status(self, adapter_ref: str) -> dict | None: ...

    def status_for_transport(self, transport_key: str) -> dict | None: ...


class ExternalHttpTransport:
    """HTTP implementation of the adapter seam against allowlisted endpoints.

    Outcome mapping per owner decision #1: 2xx → succeeded, definitive 4xx
    rejections → failed, 408/429/5xx/timeouts/connection errors → unknown.
    evidence_ref is an opaque request id from the provider (or the local
    adapter ref); raw response bodies are never returned or stored.
    T-PR3 Q3: non-loopback endpoints are resolve-and-validated and the
    connection pins to a validated address; loopback harness endpoints keep
    their documented exception.
    """

    def __init__(self, *, endpoints: dict[str, str],
                 allowed_origins: frozenset[str] | set[str],
                 credential_resolver: Callable[[str], str | None],
                 timeout_seconds: float = 5.0,
                 resolver: Callable[[str], list[str]] | None = None):
        self.endpoints = dict(endpoints)
        self.allowed_origins = frozenset(allowed_origins)
        self.credential_resolver = credential_resolver
        self.timeout_seconds = timeout_seconds
        self.resolver = resolver or _default_resolver
        self._history: list[dict] = []

    def _validated_address(self, url: str) -> str | None:
        """Resolve-and-validate for non-loopback endpoints (T-PR3 Q3).

        Returns the address to pin the connection to, or None for the
        documented loopback-harness exception (no DNS validation). Every
        resolved address of a non-loopback endpoint must be public; a single
        denied address fails closed before any connection.
        """
        hostname = urllib.parse.urlparse(url).hostname
        if hostname in LOOPBACK_HOSTS:
            return None
        if hostname is None:
            raise ValueError('TRANSPORT_ENDPOINT_NOT_ALLOWED')
        addresses = self.resolver(hostname)
        if not addresses or any(_is_denied_address(a) for a in addresses):
            raise ValueError('TRANSPORT_DNS_DENIED')
        return addresses[0]

    @staticmethod
    def _pinned_handler(url: str,
                        pinned_ip: str | None) -> urllib.request.BaseHandler:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == 'https':
            return _PinnedHTTPSHandler(pinned_ip)
        return _PinnedHTTPHandler(pinned_ip)

    def _open_pinned(self, url: str, request: urllib.request.Request):
        pinned = self._validated_address(url)
        opener = urllib.request.build_opener(_DenyRedirects,
                                             self._pinned_handler(url, pinned))
        return opener.open(request, timeout=self.timeout_seconds)

    def transport_key(self, attempt_id: str) -> str:
        """Deterministic pre-call transport identity (G-T4, shared with T-PR1)."""
        return 'IDEM-' + attempt_id

    def _endpoint(self, action: str) -> str:
        url = self.endpoints.get(action)
        if url is None:
            raise ValueError('TRANSPORT_ENDPOINT_NOT_ALLOWED')
        origin = urllib.parse.urlparse(url).netloc
        if origin not in self.allowed_origins:
            raise ValueError('TRANSPORT_ENDPOINT_NOT_ALLOWED')
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == 'https':
            return url
        if parsed.scheme == 'http' and parsed.hostname in LOOPBACK_HOSTS:
            return url
        raise ValueError('TRANSPORT_INSECURE_SCHEME')

    def _credential(self, url: str) -> str:
        credential = self.credential_resolver(url)
        if credential is None:
            raise ValueError('TRANSPORT_CREDENTIAL_UNAVAILABLE')
        return credential

    def dispatch(self, attempt: dict) -> dict:
        """POST one attempt to its allowlisted endpoint; never raises for
        provider outcomes — only boundary violations raise (pre-network)."""
        url = self._endpoint(attempt['action'])
        credential = self._credential(url)
        transport_key = attempt['transport_key']
        adapter_ref = 'HTTP-%04d' % (len(self._history) + 1)
        body = json.dumps({'attempt_id': attempt['attempt_id'],
                           'resource_id': attempt['resource_id'],
                           'payload_digest': attempt['payload_digest']}).encode('utf-8')
        request = urllib.request.Request(
            url, data=body, method='POST',
            headers={'Content-Type': 'application/json',
                     'Idempotency-Key': transport_key,
                     'Authorization': 'Bearer ' + credential})
        outcome, request_id = self._send(request, url)
        evidence_ref = ('evidence:' + request_id) if request_id \
            else 'evidence:' + adapter_ref
        self._history.append({'adapter_ref': adapter_ref,
                              'action': attempt['action'],
                              'outcome': outcome,
                              'attempt_id': attempt.get('attempt_id'),
                              'transport_key': transport_key,
                              'endpoint': url,
                              'credential_used': True,
                              'evidence_ref': evidence_ref})
        return {'outcome': outcome, 'adapter_ref': adapter_ref,
                'evidence_ref': evidence_ref}

    def _send(self, request: urllib.request.Request,
              url: str) -> tuple[str, str | None]:
        try:
            with self._open_pinned(url, request) as resp:
                request_id = resp.headers.get('X-Request-Id')
                return 'succeeded', request_id
        except urllib.error.HTTPError as exc:
            if exc.code in DENY_4XX:
                return 'failed', exc.headers.get('X-Request-Id')
            return 'unknown', exc.headers.get('X-Request-Id')
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
            return 'unknown', None

    def _lookup(self, record_ref: str, by: str) -> dict | None:
        entry = next((h for h in self._history if h[by] == record_ref), None)
        if entry is None:
            return None
        outcome = self._query_provider(entry['endpoint'], entry['transport_key'])
        return {'outcome': outcome, 'adapter_ref': entry['adapter_ref']}

    def _query_provider(self, url: str, transport_key: str) -> str:
        """Status query against the provider; failures stay 'unknown' (G-T5)."""
        request = urllib.request.Request(
            url, method='GET', headers={'X-Idempotency-Key': transport_key})
        try:
            with self._open_pinned(url, request) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                outcome = data.get('outcome')
                return outcome if outcome in ('succeeded', 'failed', 'unknown') \
                    else 'unknown'
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                ConnectionError, OSError, ValueError):
            return 'unknown'

    def status(self, adapter_ref: str) -> dict | None:
        return self._lookup(adapter_ref, 'adapter_ref')

    def status_for_transport(self, transport_key: str) -> dict | None:
        return self._lookup(transport_key, 'transport_key')
