# Final validation: every new fault run live on the lab

Branch `devops-completion`. Lab EC2 `i-04f8f4d75378a1aba`, Minikube, isolated `autosre` namespace. Runs made 2026-11-18 20:33 to 2026-11-19 00:37 UTC through the fault console,
one fault at a time, with `AUTO_REMEDIATE` switched on for that run only and confirmed off again afterwards (every run's log ends with the restore line and
`AUTO_REMEDIATE=false`). Approval to run the live batches was given in the chat. Raw results are in `s3://autosre-validation-841736140404/autosre/results/`.

What "passed" means: the alert fired, the engine named the right cause, the dispatcher's gate allowed it, the approved playbook ran, **and a separate check read the
Deployment, Service or pod afterwards and confirmed the fault was gone**. A playbook exiting with code 0 is not enough.

## Result: 17 faults and variations, all recovered live; 5 were not recovered on the first attempt

| Scenario | Outcome | Alert (s) | Incident (s) | Fix (s) | Conf. | Second opinion | Playbook |
|---|---|---:|---:|---:|---:|---|---|
| down:database (1st run) | escalated, nothing run | - | - | - | - | disagree | restore_service |
| down:database | **passed** | 56 | 113 | 30 | 0.85 | no opinion | restore_service |
| down:transaction-service | **passed** | 46 | 133 | 30 | 1.00 | no opinion | restore_service |
| down:api-gateway (1st run) | escalated | 51 | 535 | - | 0.60 | no opinion | restore_service |
| down:api-gateway (after fix) | **passed** | 46 | 61 | 35 | 1.00 | no opinion | restore_service |
| rollout:tx-crash | **passed** | 143 | 190 | 22 | 1.00 | no opinion | rollback_deployment |
| rollout:gw-crash | **passed** | 143 | 174 | 21 | 1.00 | no opinion | rollback_deployment |
| selector:database | **passed** | 71 | 117 | 26 | 0.85 | no opinion | fix_service_selector |
| selector:transaction-service | **passed** | 77 | 117 | 29 | 0.85 | no opinion | fix_service_selector |
| selector:api-gateway (1st run) | escalated | 77 | 561 | - | 0.60 | no opinion | fix_service_selector |
| selector:api-gateway (after fix) | **passed** | 77 | 123 | 25 | 1.00 | no opinion | fix_service_selector |
| paused:transaction-service | **passed** | 82 | 82 | 21 | 1.00 | no opinion | resume_deployment |
| paused:api-gateway | **passed** | 87 | 87 | 21 | 1.00 | no opinion | resume_deployment |
| svctype:database | **passed** | 72 | 72 | 21 | 1.00 | no opinion | restore_service_type |
| svctype:transaction-service | **passed** | 77 | 77 | 21 | 1.00 | no opinion | restore_service_type |
| svctype:api-gateway | **passed** | 82 | 82 | 21 | 1.00 | no opinion | restore_service_type |
| drift:replicas (1st run) | no incident reached the engine | 622 | - | - | - | - | restore_baseline_replicas |
| drift:replicas (after fix) | **passed** | 623 | 653 | 21 | 1.00 | no opinion | restore_baseline_replicas |
| pool | **passed** | 41 | 143 | 30 | 0.85 | agree | restart_service |
| memory (1st run) | escalated, nothing run | 67 | 548 | - | 0.75 | disagree | scale_replicas |
| memory (2nd run) | **passed** | 61 | 77 | 84 | 0.75 | agree | scale_replicas |
| disk | **passed** | 71 | 102 | 31 | 0.85 | agree | clear_logs |

One run per fault (two where the first failed). No success rate is claimed: this shows each playbook works against its fault at least once, not how often.

## The five first-attempt failures, each with a real cause (four were then fixed; the memory one recovered on a re-run)

1. **down:database, 1st run: the scorer vetoed a fault it had never seen.** The dispatcher requires the second opinion to `agree`. The metric scorer was trained on the
   original three causes and a Deployment with no pods has no metrics, so it said `disagree`. Fix: the scorer now answers `no_opinion` for the 14 Kubernetes-state
   causes it was not trained on, and the dispatcher treats `no_opinion` as "not a veto" for those causes only. A real `disagree` still escalates, and the original
   three causes still need `agree`. (`second-opinion/opinion.py`, `remediation-dispatcher/app.py`, test keeping the three cause lists equal.)
2. **down:api-gateway and selector:api-gateway: nothing can corroborate a gateway fault.** The deciding alert alone scores 0.6 (below the 0.75 gate); the other symptoms
   (`ErrorRateHigh`, `APILatencyHigh`) never fired for 9 minutes because a gateway with no pods or no endpoint cannot count its own failed requests. Fix: both gateway
   causes are `DEFINITIVE` like the drift causes (the deciding alert carries the whole weight). The database and transaction-service versions stay corroborated.
3. **drift:replicas: Alertmanager silently muted the alert.** `ReplicasAboveBaseline` was `severity: info`. kube-prometheus-stack's default Alertmanager config has
   an `InfoInhibitor` rule that mutes every info alert in a namespace, so the alert fired in Prometheus and Alertmanager but no webhook ever reached the engine.
   Found by reading the live Alertmanager config. Fix: severity `warning`, plus a test that no AutoSRE alert uses `info`.
4. **memory, 1st run: the scorer disagreed on one of the original causes.** The same thing happened once in an earlier session (comp13, trial 1). Nothing was executed.
   Re-run on a quiet baseline it agreed and the playbook recovered it. This is a real limit of the scorer, not hidden: its baseline window is 30 minutes, so a fault
   injected soon after another can be scored against a polluted baseline.

## Things worth knowing

* `AUTO_REMEDIATE` stayed `false` except inside each run, and the runner confirmed it off at the end of each.
* The read-only `collect_diagnostics.yml` playbook attached pod state, events and log tails to escalated incidents (checked on `unknown:errors`).
* The Services page shows many "Unknown cause" incidents for the gateway: these are early webhooks that arrive with only a generic alert before the deciding alert
  (Alertmanager groups by namespace, group_wait 30 s), then a second webhook names the cause. They inflate the "escalated" count and are noise for an on-call
  engineer. A superseded-incident rule would remove them; not done in this session.
* Screenshots: `docs/completion/screenshots/200-` to `225-` (website, Jenkins, live results, the InfoInhibitor rule, the drift run).

## Final deploy and the lab restart test

* **Jenkins build #3** of the final commit (28 commits since build #2): every stage green, including the unit tests (9 min 1 s), the image build, the deploy and the
  verify stage; `Finished: SUCCESS`, all eight deployments 1/1, `AUTO_REMEDIATE=false`. Screenshots 240 and 241.
* **Is the history really in the bucket?** Before ending the lab the bucket and the cluster held the same records: 91 incidents, 92 events and 63 playbook executions
  (`autosre/history/*.jsonl`), plus 86 result folders under `autosre/results/`.
* **Real restart.** The lab was ended (instance stopped) and started again. The instance came back with a new public address (the old one stopped working). `bring-up.sh` started
  Minikube, waited for all eight deployments, pulled the history from the bucket (91 / 92 / 63, already up to date), and confirmed `AUTO_REMEDIATE=false`. The website at the
  new address showed the same history as before: 91 incidents, 39 validation runs, 53 setting changes, no errors (screenshots 250 to 253).
* Lessons from the restart: the AWS console and CloudShell need a fresh session from the lab's own AWS link after Start Lab, the new session sometimes keeps a "cancelled
  credentials" deny for a minute or two, and the public address changes, so anything that bookmarks the old address must be updated.
* The engine now also closes incidents once Alertmanager reports that every alert of the group has cleared (a `resolve` event by Alertmanager), so early partial
  notifications stop sitting in the on-call queue; incidents recorded before this deploy stay open until someone resolves them.

## The retrained scorer, and one more live finding

* The scorer was retrained on 32 new state-fault trials plus the 53 earlier ones (two trials of each of 16 causes), learning the *kind* of fault. Held out (leave one trial out, 85 trials): right service
  98.0%, right service and fault 95.7%. Kept only the kinds at 95% or better (service down, bad rollout, paused, Service type changed, pool, disk, memory); selector faults (83%) are left out.
  Judge on the held-out windows of the kept kinds: 206 of 208 true claims backed, 206 of 208 wrong claims caught.
* Live re-check after deploying it: `memory` passed (scorer agreed, 0.75), `paused:api-gateway` passed, but **`down:database` was escalated** ("Second opinion disagrees"): the scorer's metrics pointed at
  `transaction-service`, a false veto of a correct diagnosis, although held out it had scored right. Live the engine asks at incident time with an estimated fault start, not at the fixed times of the collection runs.
* Fix: for the Kubernetes-state causes the scorer's verdict is recorded and shown but no longer gates the fix (the evidence is the cluster state itself and each playbook checks its precondition and is verified);
  the original resource causes still need `agree`. Test `test_a_state_fault_is_never_gated_by_the_scorer_but_the_verdict_is_still_recorded`.
* Re-run after the policy change: **`down:database` passed** (06:17:15, 2 min 40 s from injection): the scorer still said `disagree` (recorded, no longer gating) and the independent check confirmed the database was
  back. Live re-check summary with the retrained scorer: memory passed (scorer agreed), paused:api-gateway passed (agreed), down:database escalated on the first run (false veto) and passed after the change.
