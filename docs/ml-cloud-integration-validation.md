# AutoSRE ML + Cloud integration validation

On November 12, 2026, run `autosre-e2e-20261112T113946Z` verified the integration
Alertmanager -> Correlation Engine -> ML Second Opinion -> Remediation Dispatcher
using synthetic alerts and healthy application telemetry. No application fault was
injected and no Ansible playbook was executed. The machine-readable record is
[ml-cloud-integration-evidence.json](ml-cloud-integration-evidence.json).

## Tested deployment and image

The baseline was branch `devops-integration`, commit `e9f736f`, with the two
upstream ML validation updates retained in the working tree. Minikube used profile
`minikube`, a single node, and the containerd runtime. The verified second-opinion
Deployment used:

```yaml
image: autosre/second-opinion:ml-integration-e9f736f-20261112-a61f9c2b
imagePullPolicy: Never
```

Image identity recorded at build and rollout:

| Artifact | SHA-256 |
|---|---|
| Image manifest | `d05e71183379e94906f6f6d72adf60e12f05eb717e1877d2524add453c220665` |
| Image config / running image ID | `6ae6de9d09a120e6e5c9b9a193c7845d4a76a3f9f31cb79c7e718b02e562c395` |
| `models/own.joblib` | `fc56866e8d81045fe5805c6504e68f38cb5214511f5984d54ac5684271a8f64b` |
| `models/second_opinion.joblib` | `45c34de6fc077b0827479d5a1e16ae5c1961d6a5f441d86411d5aecc84f59450` |

The Dockerfile includes both models and sets `MODEL_PATH=models/own.joblib`.
Both models loaded during isolated image verification; the deployed service's
startup log and `/ready` confirmed that the own-data scorer loaded. Deployed
`opinion()`, `opinion_for()`, and `window_scores()` matched local source and
propagated the optional explicit `fault_start` rather than subtracting another
lead interval. The image selected scikit-learn 1.6.1, NumPy 2.5.4, pandas 3.1.1,
and joblib 1.6.0.

**This image exists only in the tested Minikube image store. It was not pushed to
a registry. Before deployment elsewhere, rebuild it from the reviewed source
into that environment and make it available on every eligible node.** With pull
policy `Never`, Kubernetes will not fetch a missing image; a manifest alone is
insufficient. Do not apply it before the image is available.

## Exact tested build procedure

From the repository root in Windows PowerShell, the build ran with the existing
Dockerfile and `second-opinion/` as its context:

```powershell
Push-Location second-opinion
try {
    & C:\minikube\minikube.exe -p minikube image build `
        -t autosre/second-opinion:ml-integration-e9f736f-20261112-a61f9c2b `
        -f Dockerfile .
} finally {
    Pop-Location
}
```

The executable path is specific to the tested Windows host; use the installed
`minikube` executable on another host. This command is documentation, not an
instruction to rebuild during review. Base-image tags and some dependency ranges
are mutable, so a later rebuild may have a different image digest. Compare source
and model hashes and repeat model-loading verification before approving it.

The existing `scripts/build-images.sh` still builds second-opinion as `:dev` and
omits the dispatcher. It does not reproduce this versioned integration deployment;
use the targeted build above for second-opinion. Full fresh-cluster provisioning
and repair of that helper are outside this validation.

## Safety preflight and synthetic procedure

Before an explicitly approved run, check all of the following without changing
configuration:

- All AutoSRE and monitoring pods are Ready; record pod UIDs and restart counts.
- Dispatcher process environment and `/health` confirm `AUTO_REMEDIATE=false`.
- Engine process environment confirms `SECOND_OPINION_MODE=shadow`; dispatcher
  integration is enabled and its Service URL is correct.
- Transaction-service has `CHAOS_ENABLED=false`; database has no enabling
  override and its inspected source defaults chaos to false.
- The verified second-opinion image, `MODEL_PATH`, model hash, source, and
  readiness match the reviewed deployment.
- There are no existing AutoSRE alerts, applicable silences, or inhibition
  sources matching the proposed labels. Inhibition rules need not be absent.
- All seven AutoSRE Prometheus targets are UP. Record exporter and Prometheus
  counter baselines, keeping engine and scorer metrics separate.

The tested localhost Alertmanager port-forward was
`http://127.0.0.1:19093`; verify `/api/v2/status` by GET before using it. Do not
assume an old forwarding process still provides a working connection.

Generate a new unique `test_run` and four alert objects immediately before POST:

| `alertname` | `service` |
|---|---|
| `DBPoolNearExhaustion` | `transaction-service` |
| `ErrorRateHigh` | `transaction-service` |
| `PodRestartsHigh` | `transaction-service` |
| `APILatencyHigh` | `api-gateway` |

Every alert has `namespace=autosre`, `severity=warning`, and the same `test_run`.
Set every `startsAt` to the current UTC time and `endsAt` to that time plus five
minutes. Serialize the four objects as a JSON array, not a webhook envelope.
Submit the array once with PowerShell 5.1 `Invoke-WebRequest`, explicit JSON
content type, and a UTF-8 byte body. In the example below, `$alerts` is that
four-object array and `$startsAt` is its common `DateTimeOffset` start time:

```powershell
$json = ConvertTo-Json -InputObject $alerts -Depth 6
$age = ([DateTimeOffset]::UtcNow - $startsAt).TotalSeconds
if ($age -lt 0 -or $age -ge 30) { throw 'Regenerate the alert timestamps' }

try {
    $response = Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:19093/api/v2/alerts' -Method Post `
        -Headers @{ Accept = 'application/json' } `
        -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($json)) `
        -MaximumRedirection 0 -TimeoutSec 15 -ErrorAction Stop
    if ($response.StatusCode -ne 200) { throw 'Alert ingestion failed' }

    # Verify the exact test_run by GET, then inspect logs and counter deltas.
    # Stop test progression if a safety check fails; finally still resolves it.
} finally {
    $end = [DateTimeOffset]::UtcNow.ToString('o')
    $alerts | ForEach-Object { $_.endsAt = $end }
    $resolvedJson = ConvertTo-Json -InputObject $alerts -Depth 6
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'http://127.0.0.1:19093/api/v2/alerts' -Method Post `
        -Headers @{ Accept = 'application/json' } `
        -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($resolvedJson)) `
        -TimeoutSec 15 -ErrorAction Stop
}
```

Keep labels and original `startsAt` unchanged during resolution. Allow the next
group interval for the resolved webhook, then verify zero alerts for this
`test_run`, the resolved counter, pod health, and unchanged restarts. The example
shows transport and cleanup structure; it is not a complete automated verifier.
Do not use `kubectl create --raw` for this POST: that transport returned HTTP 415
in the earlier attempt. The explicit localhost JSON transport returned HTTP 200.

Alertmanager groups by namespace with `group_wait=30s`, `group_interval=1m`,
and `repeat_interval=1h`. One group can produce multiple notifications. The
earlier test backdated starts by two minutes, triggering the immediate-flush
path for old alerts and producing a partial notification. Fresh timestamps
removed that trigger in the successful run. This is not a general guarantee of
atomic batch notification. Preserve escalation for real unmatched notifications;
do not drop them or reset counters to make test results look cleaner.

## Verified result and Prometheus queries

Run `autosre-e2e-20261112T113946Z` started at 11:39:46 UTC (17:09:46 IST).
The payload was 0.001 seconds old before launching its request, and PowerShell
rechecked that it was less than 30 seconds old immediately before POST.
Alertmanager accepted all four unsuppressed alerts with HTTP 200. The first
firing webhook contained all four symptoms. The engine returned HTTP 202,
identified `db_connection_pool_exhaustion` at confidence 1.0, and called the
scorer successfully. Actual own-model inference returned `disagree`, pointing
to `api-gateway`; shadow mode preserved the rule diagnosis and continued.

The dispatcher received the engine's POST and returned HTTP 200 while its
execution flag was false. The inspected deployed code follows the dry-run
branch. **Its response JSON was not captured**, and no separate direct dispatcher
confirmation was performed in this run. No playbook ran.

| Prometheus counter | Before | After |
|---|---:|---:|
| Engine firing notifications | 6 | 7 |
| Pool-exhaustion diagnoses | 2 | 3 |
| Scorer verdicts for transaction-service | 1 | 2 |
| Engine verdicts for transaction-service | 2 | 3 |
| Resolved notifications | 3 | 4 |
| Unmatched notifications | 4 | 4 |

Vetoes remained zero and the confidence gauge remained 1.0. Exporter counter
deltas also showed two Alertmanager notifications (firing and resolved) and
zero additional notification failures. There were no partial notifications.
Cleanup left no test alerts, all seven AutoSRE and six monitoring pods Ready,
unchanged pod UIDs/restart counts and Deployment specifications, and all seven
AutoSRE scrape targets UP.

Raw exporter metrics label the diagnosed application as `service`. Prometheus
adds the Kubernetes Service name as its own `service` label; the default
`honor_labels=false` preserves the conflicting application label under
`exported_service`. Query the scorer and engine independently:

```promql
second_opinion_verdicts_total{namespace="autosre",job="second-opinion",exported_service="transaction-service"}
second_opinion_verdicts_total{namespace="autosre",job="correlation-engine",exported_service="transaction-service"}
```

Do not sum both jobs to count ML calls. Each records one side of the same call.

## Rollback and limits

Before the approved second-opinion rollout, its recorded image was
`autosre/second-opinion:dev`, pull policy `IfNotPresent`, replicas 1, revision 1.
The versioned rollout became revision 2. The old image digest was
`sha256:a37d2eb182801deb85ec2659ad6534b3163d9f25de2c189fab35c4b835a9efc9`.
The old `:dev` image was left unchanged.

On the tested cluster, after separate approval, verify rollout history and old
image availability before using:

```powershell
kubectl --context=minikube -n autosre rollout history deployment/second-opinion
kubectl --context=minikube -n autosre rollout undo deployment/second-opinion --to-revision=1
kubectl --context=minikube -n autosre rollout status deployment/second-opinion --timeout=180s
```

Revision numbers are specific to that recorded history. Inspect the target
revision before any future rollback; do not assume revision 1 means the same
thing elsewhere. Reconcile the checked-in manifest after an approved rollback
so a later apply does not unintentionally restore the newer image. No rollback
was performed during validation.

The two retained updates to `evaluation/live_validate.py` and
`tests/test_live_report.py` add a webhook preflight and mocked coverage. The
local mocked suite passed 20 tests. **The live fault-injection harness was not
run for this integration test.** Its preparation enables chaos, changes cluster
configuration, and clears its capture file; it is not the safe procedure above.

This result validates transport, API integration, actual model invocation,
shadow-mode continuation, and disabled remediation behavior. It does not
validate ML gate mode, remediation execution, fault-detection accuracy, or model
correctness on real faults. Three infrastructure scrape targets (scheduler,
controller-manager, and etcd) were previously observed down; the reported seven
healthy targets refer specifically to AutoSRE. No rebuild, deployment, or live
test should be triggered merely by reviewing or committing these artifacts.
