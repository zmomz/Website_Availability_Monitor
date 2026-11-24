#!/usr/bin/env python3
"""Simple website availability monitor with webhook alerts.

- Checks each website every CHECK_INTERVAL seconds (default 300).
- A website is marked DOWN only after FAILURE_THRESHOLD (2) consecutive failed checks.
- One DOWN notification is sent when it goes down, and exactly one RECOVERY
  notification when it comes back. No duplicates while it stays down.
- Events and errors are written to monitor.log.

Webhook URL is read from --webhook-url or the MONITOR_WEBHOOK_URL environment variable.
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

WEBSITES = {
    "Cloudflare": "https://www.cloudflare.com/",
    "GitHub": "https://github.com/",
    "Wikipedia": "https://www.wikipedia.org/",
}

CHECK_INTERVAL = 300      # seconds between check rounds
REQUEST_TIMEOUT = 10      # seconds per HTTP request
FAILURE_THRESHOLD = 2     # consecutive failures before a site is DOWN
USER_AGENT = "website-monitor/1.0 (+https://github.com/)"

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")
log = logging.getLogger("monitor")


def setup_logging(log_file=LOG_FILE):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S%z")
    for handler in (logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SiteState:
    name: str
    url: str
    consecutive_failures: int = 0
    is_down: bool = False


def check_website(url, session=None, timeout=REQUEST_TIMEOUT):
    """Return (healthy, detail). Healthy means the final response is 2xx/3xx."""
    http = session or requests
    try:
        resp = http.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.ok:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP error {resp.status_code}"
    except requests.exceptions.Timeout:
        return False, f"timeout after {timeout}s"
    except requests.exceptions.ConnectionError as exc:
        return False, f"connection error: {exc.__class__.__name__}"
    except requests.exceptions.RequestException as exc:
        return False, f"request error: {exc}"


def update_state(state, healthy, detail):
    """Apply one check result. Return "DOWN", "RECOVERY" or None (no notification)."""
    if healthy:
        state.consecutive_failures = 0
        if state.is_down:
            state.is_down = False
            return "RECOVERY"
        return None

    state.consecutive_failures += 1
    if not state.is_down and state.consecutive_failures >= FAILURE_THRESHOLD:
        state.is_down = True
        return "DOWN"
    return None  # first failure, or already DOWN (duplicate suppressed)


def build_payload(event, state, detail, test=False):
    if event == "DOWN":
        message = (f"{state.name} is DOWN after {state.consecutive_failures} "
                   f"consecutive failed checks. Last error: {detail}")
    else:
        message = f"{state.name} has RECOVERED and is reachable again ({detail})."
    payload = {
        "event": event,
        "website": state.name,
        "url": state.url,
        "timestamp": utc_now(),
        "message": message,
        "consecutive_failures": state.consecutive_failures,
        "detail": detail,
    }
    if test:
        payload["test"] = True
    return payload


def send_notification(webhook_url, payload, timeout=REQUEST_TIMEOUT):
    """POST payload as JSON. Never raises; returns True on a 2xx response."""
    if not webhook_url:
        log.warning("No webhook URL configured; %s notification for %s not sent",
                    payload["event"], payload["website"])
        return False
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        log.info("Webhook delivered: %s %s (HTTP %s)", payload["event"], payload["website"], resp.status_code)
        return True
    except requests.exceptions.RequestException as exc:
        log.error("Webhook delivery FAILED for %s %s: %s", payload["event"], payload["website"], exc)
        return False


def process_check(state, healthy, detail, webhook_url, test=False):
    """Log a check result, update state and send a notification if needed.
    Returns (event, delivered) where delivered is None if nothing was sent."""
    if healthy:
        log.info("CHECK OK    %-10s %s (%s)", state.name, state.url, detail)
    else:
        log.warning("CHECK FAIL  %-10s %s (%s) [consecutive failures: %d]",
                    state.name, state.url, detail, state.consecutive_failures + 1)
    event = update_state(state, healthy, detail)
    if event:
        log.warning("EVENT %s: %s", event, state.name)
        delivered = send_notification(webhook_url, build_payload(event, state, detail, test=test))
        return event, delivered
    if not healthy and state.is_down:
        log.info("%s still DOWN; duplicate notification suppressed", state.name)
    return None, None


def run_round(states, webhook_url, session=None, checker=check_website):
    for state in states:
        try:
            healthy, detail = checker(state.url, session)
            process_check(state, healthy, detail, webhook_url)
        except Exception:  # keep going whatever happens to one site
            log.exception("Unexpected error while checking %s", state.name)


def monitor(webhook_url, interval=CHECK_INTERVAL, rounds=None):
    states = [SiteState(name, url) for name, url in WEBSITES.items()]
    session = requests.Session()
    log.info("Monitor started: %d websites, interval %ss, webhook %s",
             len(states), interval, "configured" if webhook_url else "NOT configured")
    done = 0
    while rounds is None or done < rounds:
        started = time.monotonic()
        try:
            run_round(states, webhook_url, session)
        except Exception:
            log.exception("Unexpected error in monitoring round")
        done += 1
        if rounds is not None and done >= rounds:
            break
        time.sleep(max(0, interval - (time.monotonic() - started)))
    log.info("Monitor stopped after %d round(s)", done)


def run_test_mode(webhook_url, site="Cloudflare"):
    """Feed a scripted failure/recovery sequence through the real state and
    notification functions, sending real webhooks. Returns True if it behaved."""
    state = SiteState(site, WEBSITES[site])
    # (healthy, detail, expected event)
    sequence = [
        (True,  "HTTP 200 (simulated)",              None),
        (False, "timeout after 10s (simulated)",     None),        # 1st failure: not DOWN yet
        (False, "HTTP error 503 (simulated)",        "DOWN"),      # 2nd failure: DOWN alert
        (False, "connection error (simulated)",      None),        # still down: suppressed
        (False, "HTTP error 503 (simulated)",        None),        # still down: suppressed
        (True,  "HTTP 200 (simulated)",              "RECOVERY"),  # one recovery alert
        (True,  "HTTP 200 (simulated)",              None),        # no duplicate recovery
    ]
    log.info("=== TEST MODE: simulating outage for %s (webhook %s) ===",
             site, "configured" if webhook_url else "NOT configured")
    sent, ok = [], True
    for i, (healthy, detail, expected) in enumerate(sequence, 1):
        event, delivered = process_check(state, healthy, detail, webhook_url, test=True)
        if event:
            sent.append(event)
            if not delivered:
                ok = False
                log.error("TEST step %d: %s notification was NOT delivered to the webhook", i, event)
        status = "PASS" if event == expected else "FAIL"
        ok &= event == expected
        log.info("TEST step %d: healthy=%s -> event=%s (expected %s) %s", i, healthy, event, expected, status)
    ok &= sent == ["DOWN", "RECOVERY"]
    (log.info if ok else log.error)("=== TEST MODE %s: notifications triggered = %s ===",
                                    "PASSED" if ok else "FAILED", sent)
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description="Website availability monitor")
    p.add_argument("--webhook-url", default=os.environ.get("MONITOR_WEBHOOK_URL"),
                   help="Webhook URL (default: $MONITOR_WEBHOOK_URL)")
    p.add_argument("--interval", type=int, default=CHECK_INTERVAL, help="Seconds between rounds (default 300)")
    p.add_argument("--once", action="store_true", help="Run a single check round and exit")
    p.add_argument("--test", action="store_true", help="Run test mode (simulated outage, real webhooks)")
    p.add_argument("--test-site", default="Cloudflare", choices=WEBSITES, help="Site used in test mode")
    args = p.parse_args(argv)

    setup_logging()
    if not args.webhook_url:
        log.warning("MONITOR_WEBHOOK_URL / --webhook-url not set: notifications will only be logged")

    if args.test:
        return 0 if run_test_mode(args.webhook_url, args.test_site) else 1
    try:
        monitor(args.webhook_url, args.interval, rounds=1 if args.once else None)
    except KeyboardInterrupt:
        log.info("Monitor stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
