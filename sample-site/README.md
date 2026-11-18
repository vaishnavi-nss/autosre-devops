# AutoSRE incident console (static)

A static proposal for how the website could look for its real users: on-call SWE/SRE engineers first, auditors second. Two builds of the same console:

- `AutoSRE_Incident_Console_Live.html`: built from the **logs collected so far** (`python sample-site/build_real.py`): the 31 Oct 2026 live validation (17 trials with their real timestamps, alerts and decisions), the 18 Nov 2026 fault-console runs (three simulated, four live, with the recorded times), a "Validation runs" page covering every session, and the real changes to what the automation may do. Fields a log did not record show as `-`. The "what each setting would have decided" rows are computed with the engine's own code from the recorded alerts.
- `AutoSRE_Incident_Console.html`: a year of generated data (`python sample-site/gen_data.py`) to show what the console looks like with volume.

Open either file directly (self-contained, no server or internet). Sign in with any username; the "on call" chip, owners and "Acknowledge" use the signed-in name.
`python sample-site/build_single.py [data.js] [output.html]` rebuilds a single file. The real website (`services/frontend/app.py`) is not touched by this folder.
