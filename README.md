# Website Availability Monitor

A small Python tool that checks Cloudflare, GitHub and Wikipedia every 5 minutes and posts JSON alerts to a webhook when a site goes DOWN or RECOVERS.

| Website    | URL                         |
|------------|-----------------------------|
| Cloudflare | https://www.cloudflare.com/ |
| GitHub     | https://github.com/         |
| Wikipedia  | https://www.wikipedia.org/  |

## Files

| File                 | Purpose                                                           |
|----------------------|-------------------------------------------------------------------|
| `monitor.py`         | The monitor (normal mode, `--once`, and `--test` mode)            |
| `test_monitor.py`    | Automated tests using real local HTTP servers                     |
| `capture_webhook.py` | Playwright script that screenshots the webhook.site dashboard     |
| `requirements.txt`   | Python dependencies                                               |
| `monitor.log`        | Sample execution log (created/appended on every run)             |
| `webhook-test.png`   | Screenshot of a real alert received by webhook.site               |

## 1. Install

Requires Python 3.9+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # only needed for the screenshot script
```

## 2. Generate a webhook URL

1. Open https://webhook.site/ in a browser. No account is needed.
2. The page shows **Your unique URL**, for example
   `https://webhook.site/0a1b2c3d-1234-5678-9abc-def012345678`. Copy it.
3. Leave the tab open: requests sent to that URL appear there live.

Alternatively, from a terminal: `curl -X POST https://webhook.site/token` returns JSON whose `uuid` field is your token, so the URL is `https://webhook.site/<uuid>`.

## 3. Configure the webhook URL

The URL is never hardcoded. Set it with an environment variable:

```bash
# macOS/Linux
export MONITOR_WEBHOOK_URL="https://webhook.site/<your-uuid>"
# Windows PowerShell
$env:MONITOR_WEBHOOK_URL = "https://webhook.site/<your-uuid>"
```

…or pass it on the command line with `--webhook-url <url>`, which overrides the variable. If neither is set, the monitor still runs and logs events, but warns that notifications are not being sent.

## 4. Run the monitor

```bash
python monitor.py                 # runs forever, checks every 300 s (Ctrl+C to stop)
python monitor.py --once          # one round of checks, then exit
python monitor.py --interval 60   # custom interval in seconds
```

Output goes to the console and is appended to `monitor.log` next to the script.

## 5. Run the test mode

```bash
python monitor.py --test                    # simulates an outage of Cloudflare
python monitor.py --test --test-site GitHub
```

Test mode does not touch the real websites. It feeds this scripted sequence of check results through the **same** state and notification functions the real monitor uses, and sends **real** webhook POSTs:

| Step | Simulated result          | Expected                                  |
|------|---------------------------|-------------------------------------------|
| 1    | OK                        | nothing                                   |
| 2    | timeout (failure 1)       | nothing (only one failure)                |
| 3    | HTTP 503 (failure 2)      | **DOWN** notification sent                |
| 4    | connection error          | nothing (duplicate suppressed)            |
| 5    | HTTP 503                  | nothing (duplicate suppressed)            |
| 6    | OK                        | **RECOVERY** notification sent            |
| 7    | OK                        | nothing (no second recovery)              |

It exits with code 0 only if every step matched **and** both webhooks were delivered (2xx). Test payloads carry `"test": true`. After running it, webhook.site should show exactly two requests: DOWN, then RECOVERY.

Capture the dashboard screenshot:

```bash
python capture_webhook.py "$MONITOR_WEBHOOK_URL"     # writes webhook-test.png
```

### Automated tests

```bash
python -m unittest -v test_monitor.py
```

These start local HTTP servers acting as a website (with switchable status, delay or no listener) and as a webhook receiver. They cover HTTP errors, timeouts, connection errors, the two-failure rule, duplicate prevention, single recovery, JSON payload fields, webhook delivery failures and loop resilience.

## 6. Understanding notifications

Rules, applied per website:

- A check **passes** if the final response (after redirects) is 2xx/3xx within 10 seconds.
- A check **fails** on HTTP 4xx/5xx, a timeout, or a connection/DNS/TLS error.
- One failure is only logged. **Two consecutive failures** mark the site DOWN and send one `DOWN` notification.
- While the site stays DOWN, further failures are logged as "duplicate notification suppressed". No more alerts are sent.
- The first successful check after DOWN sends exactly one `RECOVERY` notification and resets the counter.
- If the webhook can't be reached or returns an error, it is logged at ERROR level and the monitor carries on. The site's state still changes, so a failed alert is not re-sent on every later check.

Example DOWN payload (POSTed as `Content-Type: application/json`):

```json
{
  "event": "DOWN",
  "website": "Cloudflare",
  "url": "https://www.cloudflare.com/",
  "timestamp": "2026-11-24T14:30:12+00:00",
  "message": "Cloudflare is DOWN after 2 consecutive failed checks. Last error: HTTP error 503",
  "consecutive_failures": 2,
  "detail": "HTTP error 503"
}
```

A RECOVERY payload has the same fields, with `"event": "RECOVERY"` and a message saying the site is reachable again. Timestamps are UTC ISO-8601.

## Log format

```
2026-11-24 17:30:12+0300 WARNING CHECK FAIL  Cloudflare https://www.cloudflare.com/ (HTTP error 503) [consecutive failures: 2]
2026-11-24 17:30:12+0300 WARNING EVENT DOWN: Cloudflare
2026-11-24 17:30:12+0300 INFO    Webhook delivered: DOWN Cloudflare (HTTP 200)
```
