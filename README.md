# MCWorldHost automation

This isolated branch contains the MCWorldHost daily login, runtime-extension and AFK coin-farming automation.

The AFK part (`mcworldhost_afk.py` + `mcworldhost_afk.yml`) replays the `/dashboard/afk` watch actions over pure HTTP: arm a watch token, claim +3 coins every ~62s, and respect the server's 150/hour and 500/day caps. It never loads ad iframes.

The active scheduled workflow is also kept on the repository default branch (`main`), because GitHub scheduled workflows run only from the default branch. This branch is a clean, standalone snapshot and does not change the repository default branch.
