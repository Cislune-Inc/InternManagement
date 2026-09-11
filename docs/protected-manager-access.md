# Protected Mini kiosk and manager access

The shared kiosk remains on `127.0.0.1:8766`, using a standard macOS account.
It has no management function. Keep the service user logged in and use Fast User
Switching, not Log Out: current jobs run as that service user's LaunchAgents.
After a reboot the service account must be logged in before kiosk use. Do not
claim reboot persistence is solved by a standard account alone.

The manager editor binds only to loopback and requires Basic authentication on
all manager reads, writes, health detail and downloads. A missing or insecure
credential fails closed. Signed worker-portal paths retain their own identity
checks, but the retired portal is not the beta clock. The minimal `/livez` route
contains only process readiness, not operational or worker data.

Deployment preserves a random manager credential in the service user's private
`secrets` directory. Never send it in Slack, put it in a URL or command argument,
publish it, or store it on the kiosk browser. For an owner-chosen credential, run
the following in the service account's local terminal and enter it privately:

```sh
PYTHONPATH=. .venv/bin/python ops/protect_manager.py --apply --set-password
```

Restart the dashboard after changing it. Use username `manager`. Remote browser
access requires a trusted SSH tunnel to the Mini's `127.0.0.1:8765`; never proxy
the manager port to the kiosk or LAN. The SSH host/identity must be verified
before opening a tunnel. Browser logins are for trusted manager computers only.
Owner Slack remote-clock behavior is unchanged; staff remote work still requires
the owner's existing advance authorization.

Readiness requires: standard kiosk account, no remote-login/screen-sharing access
for it, no read access to DP private directories, successful kiosk navigation,
anonymous manager denial, authenticated manager read, healthy bot and backups,
and real worker verification. Do not create synthetic paid test shifts.

## Rolling handover from another clock

Preserve the previous clock's original records. A worker ends the old clock when
starting DP, using actual timestamps, without copying or deleting earlier time.
Do not start both clocks for the same work interval. Reconcile overlaps, gaps and
payroll source handling later with the worker/manager; never guess attendance.
Prior work and breaks in the current day/week must still inform the new clock's
limits. Mid-shift switching is not ready merely because PIN setup succeeds.
