# Owner pilot: Mini check-in and private handoffs

The kiosk is part of the existing Slack-only service, bound only to 127.0.0.1:8766.
Open http://127.0.0.1:8766 on the Mini itself, not on a remote worker's computer.
It starts/restarts with the bot. No separate attendance database or Gusto clock.

1. Open DP Home in your own Slack account; choose Shop check-in code.
2. Enter that eight-character code at the physical Mini within two minutes.
3. Read the name, receipt and day/week totals there. The screen clears after 20
   seconds. No Slack message is emitted by kiosk confirmation.
4. Use Slack for lunch, paid rest, updates, hours and clock-out. An onsite meal
   return also requests a kiosk code. Remote work uses an existing time-bounded
   approval; the owner's explicit company-management exception remains.

Codes are hashed at rest, single-use, bound to the Slack identity/action, and
rechecked against the current cohort and roster. New codes invalidate old codes.
The server enforces loopback peer, exact Host/Origin, CSRF, small bodies and an
attempt limit. It does not trust forwarded headers or QR/GPS claims. A clock
response failure is not evidence no time was saved: check My hours before retrying.

No technical control here proves continued physical presence or defeats a worker
sharing their code with someone onsite. Trusted administrators with SSH/remote
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
Request a code from a remote phone: it must not start time by itself. Accessing
the Mini's LAN address at port 8766 must fail. Invalid/used/expired code, CSRF,
stale enrollment and approved/unapproved remote paths have isolated test coverage.
Real worker device, break delivery and payroll acceptance remain separate gates.
