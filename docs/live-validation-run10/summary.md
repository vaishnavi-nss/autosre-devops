# Run 10: void (a deployment mistake, no data)

2026-11-12, planned as the third post-fix session: 3 trials per cause plus 5 unknown-cause, with the retrained model.
It ran 11 trials from 03:52 to 06:29 UTC and **recorded nothing**: every trial says "no incident received before the
timeout", 0 records each.

**Cause (mine).** The run script copied individual files from the branch to the instance and left out `correlate.py`.
`app.py` (new) called `correlate(firing, ESCALATE_ON_AMBIGUITY)`; `correlate.py` (old) took one argument. The engine's
log shows `TypeError: correlate() takes 1 positional argument but 2 were given` and 1,392 webhook calls, all HTTP 500,
none 202. Alertmanager was delivering; the engine was rejecting every alert.

**What it does not show.** Nothing about the scorer, the model or the gate: no incident was scored. It is not counted
as a session. The gate check still stands at 2 post-fix sessions (runs 8 and 9).

**What changed so it cannot repeat silently.**
- `scripts/aws/live-run.sh` copies whole directories from the branch instead of a file list.
- `live_validate.py` now posts a test webhook before the first trial and stops unless the engine answers 202
  (`engine_selftest`, tested), then clears the test record.
- `docs/aws-validation.md` says to read the engine log first when every trial times out.

The retrained model's fingerprint (`b565d77f`) was not confirmed on the instance either.
