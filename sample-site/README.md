# AutoSRE incident console (static demo)

A static proposal for how the website could look for its real users: on-call SWE/SRE engineers first, auditors second. The data is generated (seeded, reproducible), so this is a presentation demo, not a connection to a live system.

- Open `AutoSRE_Incident_Console.html` (one self-contained file, no server or internet needed). `index.html` + `data.js` is the same site in two files.
- Sign in with any username (the rota names are offered as shortcuts). The "on call" chip, owners and "Acknowledge" use the signed-in name; nothing is stored except the name in the browser.
- `python sample-site/gen_data.py` regenerates `data.js` (about 670 incidents over 12 months, ending 2026-11-18 12:00 UTC); `python sample-site/build_single.py` rebuilds the single file.

Pages: Incidents (needs-you list, KPIs, filters, detail drawer), Trends, Services, Playbooks, Settings & changes. The real website (`services/frontend/app.py`) is not touched by this folder.
