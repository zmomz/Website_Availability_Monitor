"""Tests for monitor.py using real local HTTP servers (no mocks of requests).

Run:  python -m unittest -v test_monitor.py
"""
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor


class Receiver:
    """Local webhook receiver + fake website with controllable status."""

    def __init__(self):
        self.requests = []
        self.site_status = 200
        self.webhook_status = 200
        self.site_delay = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(outer.site_delay)
                self.send_response(outer.site_status)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"content_type": self.headers.get("Content-Type"),
                                       "json": json.loads(body)})
                self.send_response(outer.webhook_status)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver()
        self.hook = self.rx.base + "/hook"
        self.tmp = tempfile.TemporaryDirectory()
        monitor.setup_logging(os.path.join(self.tmp.name, "test.log"))
        logging.getLogger("monitor").handlers[1].setLevel(logging.CRITICAL)  # quiet console

    def tearDown(self):
        self.rx.close()
        for h in logging.getLogger("monitor").handlers:
            h.close()
        self.tmp.cleanup()

    def site(self):
        return monitor.SiteState("Local", self.rx.base + "/")

    def check(self, state):
        healthy, detail = monitor.check_website(state.url, timeout=1)
        return monitor.process_check(state, healthy, detail, self.hook)[0]

    # --- check_website -------------------------------------------------
    def test_healthy_and_http_error(self):
        self.assertEqual(monitor.check_website(self.rx.base, timeout=2), (True, "HTTP 200"))
        self.rx.site_status = 503
        self.assertEqual(monitor.check_website(self.rx.base, timeout=2), (False, "HTTP error 503"))

    def test_timeout(self):
        self.rx.site_delay = 2
        ok, detail = monitor.check_website(self.rx.base, timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("timeout", detail)

    def test_connection_error(self):
        ok, detail = monitor.check_website("http://127.0.0.1:1/", timeout=2)
        self.assertFalse(ok)
        self.assertIn("connection error", detail)

    # --- state machine + real webhooks --------------------------------
    def test_two_consecutive_failures_required(self):
        s = self.site()
        self.rx.site_status = 500
        self.assertIsNone(self.check(s))
        self.assertEqual(self.rx.requests, [])          # one failure: no alert
        self.rx.site_status = 200
        self.assertIsNone(self.check(s))                # resets counter
        self.rx.site_status = 500
        self.assertIsNone(self.check(s))                # failure #1 again
        self.assertEqual(self.check(s), "DOWN")         # failure #2 -> DOWN
        self.assertEqual(len(self.rx.requests), 1)

    def test_duplicate_prevention_and_single_recovery(self):
        s = self.site()
        self.rx.site_status = 503
        events = [self.check(s) for _ in range(6)]
        self.assertEqual(events, [None, "DOWN", None, None, None, None])
        self.rx.site_status = 200
        events = [self.check(s) for _ in range(3)]
        self.assertEqual(events, ["RECOVERY", None, None])
        self.assertEqual([r["json"]["event"] for r in self.rx.requests], ["DOWN", "RECOVERY"])

    def test_payload_is_json_with_required_fields(self):
        s = self.site()
        self.rx.site_status = 503
        self.check(s); self.check(s)
        req = self.rx.requests[0]
        self.assertEqual(req["content_type"], "application/json")
        for key in ("event", "website", "url", "timestamp", "message"):
            self.assertIn(key, req["json"])
        self.assertEqual(req["json"]["url"], s.url)

    def test_webhook_failure_handled(self):
        payload = monitor.build_payload("DOWN", self.site(), "x")
        self.rx.webhook_status = 500
        self.assertFalse(monitor.send_notification(self.hook, payload))
        self.assertFalse(monitor.send_notification("http://127.0.0.1:1/hook", payload, timeout=2))
        self.assertFalse(monitor.send_notification(None, payload))

    def test_down_state_kept_when_webhook_fails(self):
        s = self.site()
        self.rx.site_status = 503
        self.rx.webhook_status = 500
        self.check(s); self.check(s)
        self.assertTrue(s.is_down)
        self.assertIsNone(self.check(s))  # no retry spam while still down

    def test_round_continues_after_unexpected_error(self):
        states = [monitor.SiteState("Boom", "boom"), self.site()]

        def checker(url, session):
            if url == "boom":
                raise RuntimeError("unexpected")
            return monitor.check_website(url, session, timeout=2)

        self.rx.site_status = 503
        monitor.run_round(states, self.hook, checker=checker)
        monitor.run_round(states, self.hook, checker=checker)
        self.assertTrue(states[1].is_down)             # second site still processed
        self.assertEqual(len(self.rx.requests), 1)

    def test_monitor_loop_runs_multiple_rounds(self):
        original = monitor.WEBSITES
        monitor.WEBSITES = {"Local": self.rx.base + "/"}
        try:
            self.rx.site_status = 503
            monitor.monitor(self.hook, interval=0, rounds=3)
        finally:
            monitor.WEBSITES = original
        self.assertEqual([r["json"]["event"] for r in self.rx.requests], ["DOWN"])

    def test_test_mode_fails_when_webhook_unreachable(self):
        self.assertFalse(monitor.run_test_mode("http://127.0.0.1:1/hook"))

    def test_test_mode_sends_exactly_down_and_recovery(self):
        self.assertTrue(monitor.run_test_mode(self.hook))
        events = [r["json"] for r in self.rx.requests]
        self.assertEqual([e["event"] for e in events], ["DOWN", "RECOVERY"])
        self.assertTrue(all(e["test"] for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
