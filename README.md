# MCWorldHost automation

This isolated branch contains all three MCWorldHost automations:

| Automation | Script | Workflow | What it does |
|---|---|---|---|
| Daily check-in + renewal | `mcworldhost_daily_login.py` | `mcworldhost_daily_login.yml` (hourly at :13) | Claims the daily login reward, then buys the 7-day runtime extension once 500 Coins are available |
| AFK coin farming | `mcworldhost_afk.py` | `mcworldhost_afk.yml` (hourly at :47) | Replays the `/dashboard/afk` watch actions over pure HTTP: arm a watch token, claim +3 coins every ~62s, respects the 150/hour and 500/day caps; never loads ad iframes |

Both workflows share the `MCWH_EMAIL` / `MCWH_PASSWORD` repository secrets.

The active scheduled workflows are also kept on the repository default branch (`main`), because GitHub scheduled workflows run only from the default branch. This branch is a clean, standalone snapshot and does not change the repository default branch.
