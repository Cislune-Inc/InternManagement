# Owner pilot: Mini check-in and private handoffs

The kiosk is part of the existing Slack-only service, bound only to 127.0.0.1:8766.
Open http://127.0.0.1:8766 on the Mini itself, not on a remote worker's computer.
It starts/restarts with the bot. No separate attendance database or Gusto clock.

1. At the Mini, choose your name and enter your personal six-digit PIN.
2. Tap Start / return to work. Lunch, paid rest and clock-out are also available.
3. Read the receipt; the screen clears after 15 seconds, or 30 seconds of idle
   entry. No Slack message is emitted by a kiosk action. Totals stay in Slack.
4. Use Slack for work updates, hours and corrections. Remote work uses an existing
   time-bounded approval; the owner's company-management exception remains.

One-time setup: the worker sends `kiosk setup` from their own authenticated Slack
account, then uses Set / reset PIN on the Mini within ten minutes. Alternatively,
a trusted local operator verifies identity and runs
`PYTHONPATH=. .venv/bin/python ops/kiosk_pin_setup.py --owner --apply`
or targets one enrolled identity with `--slack-id ID`. The operator never sees or
chooses the PIN. The worker types it twice on the Mini; setup never starts time.
Reset keeps the old PIN valid until its replacement is saved. Setup windows are
one-use, time-limited and actor-scoped; old Slack timestamps cannot extend them.

PINs are salted/scrypt-hashed, never plaintext or clock-event metadata. Five wrong
attempts lock that identity for 15 minutes, durably across restarts. The current
cohort/active roster is checked on every action. Only enrolled names appear.
The server enforces loopback peer, exact Host/Origin, CSRF, small bodies and an
attempt limit. It does not trust forwarded headers or QR/GPS claims. No QR or
temporary-code login remains; historical code records are preserved, not active.
A clock
response failure is not evidence no time was saved: check My hours before retrying.

No technical control here proves continued physical presence or defeats a worker
sharing their PIN with someone onsite. Trusted administrators with SSH/remote
desktop can also reach the Mini. A separate restricted kiosk account, manager
editor access controls and a no-proxy rule are required before staff rollout.
The existing network manager editor must not be exposed as a worker hours page.
Do not disable administrator remote recovery as an unreviewed deployment step.

## Useful updates, without publication

Use `work <project>: <plan>`, then `work update <actual result>` and optionally
`work next <next step or blocker>`. Preview update / `work draft` displays only
worker-supplied results and source references, never inferred completion or AI
approval. `work confirm SH-id` saves the exact revision for private owner review.
`work handoffs` shows the owner's review queue (other workers see only their own).
Changed revisions invalidate older handoffs; originals stay preserved.

Confirmation never authorizes a channel post. Channel publication is disabled.
Erik must explicitly request any assistant Slack/email send. Worker updates and
kiosk actions never depend on prose quality or an AI response to record hours.

## Acceptance

Use only a genuine owner work block for live acceptance; no synthetic staff hours.
Confirm the kiosk shows the correct identity and actual confirmation timestamp,
then end from Slack and reconcile totals. Reopen DP Home to refresh controls.
Request onsite start from Slack: it must not start time by itself. Accessing
the Mini's LAN address at port 8766 must fail. Wrong PIN, durable lockout, scoped
setup/reset, duplicate clock requests, CSRF,
stale enrollment and approved/unapproved remote paths have isolated test coverage.
Real worker device, break delivery and payroll acceptance remain separate gates.
