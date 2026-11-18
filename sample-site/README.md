# AutoSRE sample website (static, for review)

A proposal for how the website could look for its real users: SWE/SRE on-call engineers first, auditors second.
**Everything is sample data**, invented and generated (seeded, reproducible). Nothing here is a real incident and no button changes anything.

- Open `AutoSRE_Sample_Site.html` (one self-contained file, no server, no internet needed). `index.html` + `data.js` is the same site in two files.
- `python sample-site/gen_data.py` regenerates `data.js` (about 670 incidents over 12 months, ending 2026-11-18 12:00 UTC).
- `python sample-site/build_single.py` rebuilds the single file.

Pages: Incidents (needs-you list, KPIs, filters, detail drawer with timeline, evidence and what each setting would have decided), Trends, Services, Playbooks, Settings & changes.
The real website (`services/frontend/app.py`) is not touched by this folder.
