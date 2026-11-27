"""M5.5 T-PR2: contract tests over a localhost fake provider.

Real HTTP over the loopback interface, ZERO external network (owner decision
#5): the fake provider is a stdlib http.server in a thread. Proves the
transport seam rules end to end: allowlist + scheme + credential denials are
pre-network (G-T6/G-T3), failure mapping follows the owner decisions (G-T5),
the Idempotency-Key is the T-PR1 transport identity (G-T4), redirects are
never followed (SSRF disposition 2026-11-27), secrets and raw provider
payloads never persist (G-T3/G-T7) and every outcome path audits (G-T9).
"""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from company_os.operations import ExternalOperationManager, ScriptedExternalAdapter
from company_os.store import Store, Actor
from company_os.policy_store import SQLitePolicyResolver
from company_os.transport import ExternalAdapter, ExternalHttpTransport

NOW = datetime.now(timezone.utc)
RAW_BODY_MARKER = 'RAW-PROVIDER-PAYLOAD-MARKER'
SECRET = 's3cr3t-fixture-token'


class FakeProviderHandler(BaseHTTPRequestHandler):
    """Scriptable fake provider; behaviors are keyed by request path."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length:
            self.rfile.read(length)
        server = self.server
        server.post_count += 1
        key = self.headers.get('Idempotency-Key')
        if key in server.records:
            # Idempotent replay: the provider already processed this key.
            self._respond(200, {'outcome': server.records[key]})
            return
        behavior = server.behaviors.get(self.path, 'ok')
        if behavior == 'timeout':
            time.sleep(2.0)
            server.records[key] = 'succeeded'
            server.settled[key] = 'succeeded'
            self._respond(200, {'outcome': 'succeeded'})
            return
        if behavior == 'redirect':
            self.send_response(302)
            self.send_header('Location',
                             'http://%s/redirect-target' % server.other_port)
            self.end_headers()
            return
        if behavior in ('error400', 'error401', 'error403', 'error404'):
            self._respond(int(behavior[5:]), {'error': RAW_BODY_MARKER})
            return
        if behavior == 'error429':
            self._respond(429, {'error': RAW_BODY_MARKER})
            return
        if behavior == 'error500':
            self._respond(500, {'error': RAW_BODY_MARKER})
            return
        if behavior == 'flaky':
            # The provider accepted and settled the call, but the response is
            # a 503 the ledger can only read as 'unknown' — the crash-window
            # analog over HTTP; a later status query settles it.
            server.records[key] = 'succeeded'
            server.settled[key] = 'succeeded'
            self._respond(503, {'error': RAW_BODY_MARKER})
            return
        server.records[key] = 'succeeded'
        server.settled[key] = 'succeeded'
        self._respond(200, {'outcome': 'succeeded'})

    def do_GET(self):
        server = self.server
        key = self.headers.get('X-Idempotency-Key')
        if key in server.settled:
            self._respond(200, {'outcome': server.settled[key]})
        else:
            self._respond(404, {'error': 'no record'})

    def _respond(self, code, payload):
        payload = dict(payload)
        payload['raw_marker'] = RAW_BODY_MARKER
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('X-Request-Id', 'REQ-%d' % self.server.post_count)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeProviderServer:
    """Localhost-only fake provider; binds an ephemeral port on 127.0.0.1."""

    def __init__(self):
        self.behaviors: dict[str, str] = {}
        self.records: dict[str, str] = {}
        self.settled: dict[str, str] = {}
        self.other_port: int = 0
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), FakeProviderHandler)
        self._server.behaviors = self.behaviors
        self._server.records = self.records
        self._server.settled = self.settled
        self._server.post_count = 0
        self._server.other_port = self.other_port
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def post_count(self) -> int:
        """Shared counter lives on the server object the handler mutates."""
        return self._server.post_count

    def url(self, behavior: str) -> str:
        return 'http://127.0.0.1:%d/provider/%s' % (self.port, behavior)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


class TransportContractTests(unittest.TestCase):
    """Transport-level contract tests (no ledger, no network beyond loopback)."""

    def setUp(self):
        self.provider = FakeProviderServer()
        self.provider.other_port = 0
        self.transport = ExternalHttpTransport(
            endpoints={'external.publish': self.provider.url('publish')},
            allowed_origins={'127.0.0.1:%d' % self.provider.port},
            credential_resolver=lambda url: SECRET)

    def tearDown(self):
        self.provider.stop()

    def _transport_key(self):
        return self.transport.transport_key('ATT-1')

    def test_protocol_conformance_scripted_and_http(self):
        self.assertIsInstance(self.transport, ExternalAdapter)
        self.assertIsInstance(ScriptedExternalAdapter(), ExternalAdapter)

    def test_idempotency_key_sent_and_derived_from_attempt(self):
        result = self.transport.dispatch({
            'action': 'external.publish', 'attempt_id': 'ATT-1',
            'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
            'transport_key': self._transport_key()})
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(self.provider.records.get('IDEM-ATT-1'), 'succeeded')

    def test_duplicate_key_replays_recorded_outcome(self):
        attempt = {'action': 'external.publish', 'attempt_id': 'ATT-1',
                   'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                   'transport_key': self._transport_key()}
        first = self.transport.dispatch(attempt)
        second = self.transport.dispatch(attempt)
        self.assertEqual(first['outcome'], 'succeeded')
        self.assertEqual(second['outcome'], 'succeeded')
        self.assertEqual(self.provider.post_count, 2)
        self.assertLess(self.provider.records and len(self.provider.records), 2)

    def test_5xx_maps_unknown_then_status_settles(self):
        self.provider.behaviors['/provider/publish'] = 'error500'
        result = self.transport.dispatch({
            'action': 'external.publish', 'attempt_id': 'ATT-1',
            'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
            'transport_key': self._transport_key()})
        self.assertEqual(result['outcome'], 'unknown')
        status = self.transport.status_for_transport('IDEM-ATT-1')
        self.assertIsNotNone(status)
        self.assertEqual(status['outcome'], 'unknown')

    def test_flaky_503_records_settled_outcome_for_recovery(self):
        self.provider.behaviors['/provider/publish'] = 'flaky'
        result = self.transport.dispatch({
            'action': 'external.publish', 'attempt_id': 'ATT-1',
            'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
            'transport_key': self._transport_key()})
        self.assertEqual(result['outcome'], 'unknown')
        status = self.transport.status_for_transport('IDEM-ATT-1')
        self.assertEqual(status['outcome'], 'succeeded')

    def test_timeout_maps_unknown_and_provider_settles_later(self):
        self.provider.behaviors['/provider/publish'] = 'timeout'
        transport = ExternalHttpTransport(
            endpoints={'external.publish': self.provider.url('publish')},
            allowed_origins={'127.0.0.1:%d' % self.provider.port},
            credential_resolver=lambda url: SECRET,
            timeout_seconds=0.4)
        started = time.monotonic()
        result = transport.dispatch({
            'action': 'external.publish', 'attempt_id': 'ATT-1',
            'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
            'transport_key': transport.transport_key('ATT-1')})
        elapsed = time.monotonic() - started
        self.assertEqual(result['outcome'], 'unknown')
        self.assertLess(elapsed, 1.5)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and 'IDEM-ATT-1' not in self.provider.settled:
            time.sleep(0.05)
        status = transport.status_for_transport('IDEM-ATT-1')
        self.assertEqual(status['outcome'], 'succeeded')

    def test_4xx_maps_failed(self):
        for behavior in ('error400', 'error401', 'error403', 'error404'):
            self.provider.behaviors['/provider/publish'] = behavior
            result = self.transport.dispatch({
                'action': 'external.publish', 'attempt_id': 'ATT-%s' % behavior,
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': self.transport.transport_key('ATT-%s' % behavior)})
            self.assertEqual(result['outcome'], 'failed', behavior)

    def test_429_maps_unknown(self):
        self.provider.behaviors['/provider/publish'] = 'error429'
        result = self.transport.dispatch({
            'action': 'external.publish', 'attempt_id': 'ATT-1',
            'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
            'transport_key': self._transport_key()})
        self.assertEqual(result['outcome'], 'unknown')

    def test_redirect_denied_no_followup_request(self):
        other = FakeProviderServer()
        self.provider.other_port = other.port
        self.provider.behaviors['/provider/publish'] = 'redirect'
        try:
            result = self.transport.dispatch({
                'action': 'external.publish', 'attempt_id': 'ATT-1',
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': self._transport_key()})
            self.assertEqual(result['outcome'], 'unknown')
            self.assertEqual(other.post_count, 0)
        finally:
            other.stop()

    def test_non_allowlisted_action_denied_pre_network(self):
        before = self.provider.post_count
        with self.assertRaises(ValueError) as caught:
            self.transport.dispatch({
                'action': 'unknown.action', 'attempt_id': 'ATT-1',
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': self._transport_key()})
        self.assertEqual(str(caught.exception), 'TRANSPORT_ENDPOINT_NOT_ALLOWED')
        self.assertEqual(self.provider.post_count, before)

    def test_off_origin_endpoint_denied_pre_network(self):
        transport = ExternalHttpTransport(
            endpoints={'external.publish': 'http://127.0.0.1:9/provider/publish'},
            allowed_origins={'127.0.0.1:%d' % self.provider.port},
            credential_resolver=lambda url: SECRET)
        with self.assertRaises(ValueError) as caught:
            transport.dispatch({
                'action': 'external.publish', 'attempt_id': 'ATT-1',
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': transport.transport_key('ATT-1')})
        self.assertEqual(str(caught.exception), 'TRANSPORT_ENDPOINT_NOT_ALLOWED')
        self.assertEqual(self.provider.post_count, 0)

    def test_insecure_scheme_denied(self):
        transport = ExternalHttpTransport(
            endpoints={'external.publish': 'http://provider.example.com/publish'},
            allowed_origins={'provider.example.com'},
            credential_resolver=lambda url: SECRET)
        with self.assertRaises(ValueError) as caught:
            transport.dispatch({
                'action': 'external.publish', 'attempt_id': 'ATT-1',
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': transport.transport_key('ATT-1')})
        self.assertEqual(str(caught.exception), 'TRANSPORT_INSECURE_SCHEME')
        self.assertEqual(self.provider.post_count, 0)

    def test_missing_credential_denied_pre_call(self):
        transport = ExternalHttpTransport(
            endpoints={'external.publish': self.provider.url('publish')},
            allowed_origins={'127.0.0.1:%d' % self.provider.port},
            credential_resolver=lambda url: None)
        with self.assertRaises(ValueError) as caught:
            transport.dispatch({
                'action': 'external.publish', 'attempt_id': 'ATT-1',
                'resource_id': 'external:publish', 'payload_digest': 'sha256:x',
                'transport_key': transport.transport_key('ATT-1')})
        self.assertEqual(str(caught.exception), 'TRANSPORT_CREDENTIAL_UNAVAILABLE')
        self.assertEqual(self.provider.post_count, 0)


class TransportLedgerContractTests(unittest.TestCase):
    """Manager-level contract tests: the transport behind the real ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / 'transport-ledger.db', fixture_bootstrap=True)
        d = self.s.db
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)', ('C', 'active'))
        d.execute('INSERT INTO positions VALUES (?,?,?,?)', ('C', 'P', 'role.engineer@1', None))
        d.execute('INSERT INTO agents VALUES (?,?,?)', ('C', 'AGT', 'permanent'))
        d.execute('INSERT INTO appointments VALUES (?,?,?,?,?)', ('C', 'APT', 'AGT', 'P', 'active'))
        self.r = SQLitePolicyResolver(self.s)
        d.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)', ('C', 'APT', 'cap.dispatch'))
        d.execute('INSERT INTO company_policy_versions VALUES (?,?,?)', ('C', 'pol@1', 1))
        rule = {'action': 'operation.dispatch', 'eligible_roles': ['role.engineer@1'],
                'required_capabilities': ['cap.dispatch'], 'allowed_task_states': [],
                'allowed_company_states': ['active'], 'task_required': False,
                'approval_required': False, 'risk_level': 'L2'}
        d.execute('INSERT INTO company_policy_rules VALUES (?,?,?,?,?)',
                  ('C', 'pol@1', 'operation.dispatch', json.dumps(rule), 3))
        d.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                  ('C', 'G-EXT', 'APT', None, 'operation.dispatch', 'external:publish',
                   (NOW + timedelta(hours=1)).isoformat(), 5, 1))
        d.execute('INSERT INTO policy_budgets VALUES (?,?)', ('C', 10))
        self.provider = FakeProviderServer()
        self.transport = ExternalHttpTransport(
            endpoints={'external.publish': self.provider.url('publish')},
            allowed_origins={'127.0.0.1:%d' % self.provider.port},
            credential_resolver=lambda url: SECRET)
        self.manager = ExternalOperationManager(self.s, self.transport)
        self.actor = Actor('C', 'AGT', 'APT')
        self.payload = {'attempt_id': 'ATT-1', 'target_action': 'external.publish',
                        'target_resource': 'external:publish',
                        'target_payload': {'note': 'x'}}

    def tearDown(self):
        self.s.close()
        self.provider.stop()
        self.tmp.cleanup()

    def _run(self, payload, key='k1'):
        with self.s.tx():
            info = self.r.enforce(self.actor, 'operation.dispatch', payload)
            self.manager.authorize_dispatch(self.actor, payload, info, key)
        call = self.manager.execute_effect(self.actor, payload, info)
        with self.s.tx():
            return self.manager.settle_dispatch(self.actor, payload, call, key)

    def _audit_payloads(self):
        rows = self.s.db.execute('SELECT payload FROM audit_events').fetchall()
        return [r['payload'] for r in rows]

    def _db_values(self):
        values = []
        for row in self.s.db.execute('SELECT * FROM operation_attempts'):
            values.extend(str(v) for v in tuple(row))
        for row in self.s.db.execute('SELECT * FROM audit_events'):
            values.extend(str(v) for v in tuple(row))
        for row in self.s.db.execute('SELECT * FROM policy_gateway_receipts'):
            values.extend(str(v) for v in tuple(row))
        return values

    def test_secret_never_persisted_anywhere(self):
        self.provider.behaviors['/provider/publish'] = 'ok'
        result = self._run(self.payload)
        self.assertEqual(result['state'], 'succeeded')
        for value in self._db_values():
            self.assertNotIn(SECRET, value)
        for entry in self.transport._history:
            self.assertNotIn(SECRET, str(entry))
            self.assertIs(entry.get('credential_used'), True)

    def test_raw_response_body_never_persisted(self):
        self.provider.behaviors['/provider/publish'] = 'error400'
        result = self._run(self.payload)
        self.assertEqual(result['state'], 'failed')
        for value in self._db_values():
            self.assertNotIn(RAW_BODY_MARKER, value)

    def test_audit_coverage_across_outcomes(self):
        self.provider.behaviors['/provider/publish'] = 'ok'
        self._run(self.payload)
        self.provider.behaviors['/provider/publish'] = 'error500'
        payload2 = {'attempt_id': 'ATT-2', 'target_action': 'external.publish',
                    'target_resource': 'external:publish', 'target_payload': {'n': 2}}
        self._run(payload2, key='k2')
        self.provider.behaviors['/provider/publish'] = 'error400'
        payload3 = {'attempt_id': 'ATT-3', 'target_action': 'external.publish',
                    'target_resource': 'external:publish', 'target_payload': {'n': 3}}
        self._run(payload3, key='k3')
        events = {}
        for row in self.s.db.execute(
                'SELECT event_type,count(*) AS n FROM audit_events GROUP BY event_type'):
            events[row['event_type']] = row['n']
        for event in ('operation.authorized', 'operation.dispatched'):
            self.assertEqual(events.get(event, 0), 3, event)
        for event in ('operation.succeeded', 'operation.unknown',
                      'operation.failed'):
            self.assertEqual(events.get(event, 0), 1, event)
        self.assertEqual(events.get('operation.cost_refunded', 0), 1)
        self.assertEqual(self.s.db.execute(
            "SELECT state FROM operation_attempts WHERE id='ATT-2'").fetchone()['state'],
            'unknown')


if __name__ == '__main__':
    unittest.main()
