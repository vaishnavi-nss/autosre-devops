# Running the live validation on AWS Academy Learner Lab

How to stand up a throwaway single-node cluster on one EC2 instance, run the fault
injection trials against it, and collect the results. Written from doing it, so the
constraints below are the ones that actually bit.

**What this produces:** `incidents.jsonl` + `trials.jsonl` + `report.md` — real alerts,
delivered by the real Prometheus → Alertmanager → engine pipeline, for faults injected
through the `CHAOS_ENABLED` endpoints. See [evaluation.md](evaluation.md) for how to read
the numbers, and what they do and do not show.

## What Learner Lab will and won't let you do

| Constraint | Consequence |
|---|---|
| **Instance types above `large` are denied** by an explicit policy (`ec2:RunInstances`) | `t3.xlarge` fails with *"explicit deny in an identity-based policy"*. Use `t3.large` (2 vCPU / 8 GB). The bootstrap sizes minikube from the host for this reason. |
| **No IAM role creation** | EKS is out. Use the existing `LabInstanceProfile`, which can read and write S3 and use SSM. |
| **Session lasts ~4 hours** | Results are synced to S3 every minute, so an expired session loses nothing that was already recorded. Check the timer on the Vocareum page before starting. |
| **Instances are stopped when the session ends** | Don't leave Grafana up expecting it to persist. |

## One-time setup

1. **Start the lab** at `labs.vocareum.com`, wait for the AWS indicator to go green, and
   open the AWS console from there. A console URL typed directly fails with *Session
   error*: the sign-in is federated through that tab.
2. **Make a private S3 bucket** (block all public access stays on). Name it something
   unique, e.g. `autosre-validation-<account-id>`.
3. **Package and upload the repo** (the instance pulls it; there is no GitHub remote):

   ```bash
   cd ..                    # the directory that CONTAINS autosre/
   tar -czf autosre.tar.gz --exclude=.git --exclude=__pycache__ --exclude=.venv autosre
   ```

   Upload `autosre.tar.gz` to the bucket. GNU `tar` reads `C:` in a path as a remote host,
   so use a relative output path on Windows.
4. **Security group**: one inbound rule, TCP 3000 from *your* IP only (use the "My IP"
   source). Do not leave the wizard's default *SSH from Anywhere* in place. Nothing else is
   needed: you can reach the instance through Systems Manager instead (below).

## Launching

The instance bootstraps itself from user-data. Settings that matter:

| Setting | Value |
|---|---|
| AMI | Ubuntu Server **24.04** LTS. The default may be a newer release. |
| Type | `t3.large` (the largest allowed) |
| Storage | 40 GiB gp3 |
| Key pair | none needed |
| IAM instance profile | `LabInstanceProfile` |
| Metadata | IMDSv2, hop limit 2 |

User-data (replace the bucket name):

```bash
#!/bin/bash
export DEBIAN_FRONTEND=noninteractive AWS_DEFAULT_REGION=us-east-1
apt-get update -y && apt-get install -y unzip curl
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install
aws s3 cp s3://YOUR-BUCKET/autosre.tar.gz /tmp/autosre.tar.gz
tar -xzf /tmp/autosre.tar.gz -C /home/ubuntu
PER_CAUSE=6 UNKNOWN=4 DEADLINE_MIN=150 bash /home/ubuntu/autosre/scripts/aws/bootstrap.sh YOUR-BUCKET >> /var/log/autosre-userdata.log 2>&1
```

`DEADLINE_MIN` stops *starting* new trials after that many minutes, so a run always ends
cleanly inside the session. Budget **8-9 minutes per trial**: detection takes 1-2 minutes, the
fault is then held for 2, and the rest is waiting for the previous trial's alerts to clear.
They clear slowly because the rules use `rate(...[5m])`, so `APILatencyHigh` and
`ErrorRateHigh` keep firing for up to ~5 minutes after a fault ends (replacing the pods does
not help: their old samples stay inside the 5-minute range). A 4-hour session therefore fits
about 20 trials, not the 30+ the arithmetic suggests.

> **Console gotcha:** when you change the AMI in the launch wizard, a *"Some of your
> current settings will be changed"* dialog appears. Until you click **Confirm**, the page
> looks stuck and every later click lands on a re-rendering form.

Bootstrapping takes about 10 minutes (packages, minikube, a 7-minute Helm install), then
the trials start.

## Watching it

Everything the instance does is mirrored to S3, so you never need to log in:

```bash
# in AWS CloudShell (free, in the console, already authenticated)
B=s3://YOUR-BUCKET
aws s3 cp $B/progress/bootstrap.log - | grep -E '^===|FAILED' | tail     # which stage
aws s3 cp $B/progress/bootstrap.log - | tail -20                         # latest trial output
aws s3 cp $B/results/STATUS -                                            # RUNNING / DONE / FAILED
```

To run a command on the instance without SSH (needs the instance profile; the instance
registers as *Online*):

```bash
I=i-xxxxxxxxxxxxxxxxx
r(){ ID=$(aws ssm send-command --instance-ids $I --document-name AWS-RunShellScript \
  --parameters commands="[\"$1\"]" --query Command.CommandId --output text); sleep ${2:-6};
  aws ssm get-command-invocation --command-id $ID --instance-id $I \
  --query '[Status,StandardOutputContent,StandardErrorContent]' --output text; }
r 'sudo -u ubuntu -H kubectl get pods -A'
```

Keep the command free of double quotes and backslashes (they sit inside a JSON string), or
base64 the script and pipe it through `base64 -d`.

Grafana is at `http://<instance-public-ip>:3000/d/autosre-main` — anonymous, read-only,
reachable only from the IP in the security group.

## The second opinion in the run

`build-images.sh` now also builds `autosre/second-opinion:dev` (scikit-learn, about 2 extra
minutes on a `t3.large`; the trained model is `second-opinion/models/second_opinion.joblib`, so
the tarball must include it) and `deploy.sh` waits for it. The engine is configured to call it in
`shadow` mode: it annotates and counts, and never changes a decision.

Before the trials, the bootstrap asks the scorer one question and writes the reply to
`results/second_opinion_smoke.txt`. Any JSON verdict, even `no_opinion`, means the image, the model
and the Prometheus queries work; a connection error means stop and read `second-opinion.log`.

During each trial the harness also probes the scorer (through `deploy/loadgen`, which has `curl`)
with the true diagnosis and a deliberately wrong one. Results:

| File | What it adds |
|---|---|
| `trials.jsonl` | a `probes` list per trial: the verdicts for the true and the wrong claim |
| `incidents.jsonl` | each record now has a `response` with the engine's decision and the scorer's verdict |
| `report.md` | a "Second opinion" section: false vetoes, wrong claims caught, abstentions |
| `second-opinion.log` | the scorer's log, with the reason for every `no_opinion` |

Read the report's three numbers before ever setting `SECOND_OPINION_MODE=gate`. The scorer needs
about five minutes of Prometheus history for its baseline, which the bootstrap's warm-up covers.

## Problems this run hit, and the fixes

| Symptom | Cause | Fix |
|---|---|---|
| Bootstrap died right after building images | A cosmetic `docker images` listing hit a dropped connection to minikube's Docker daemon; `set -e` made it fatal | Listing is non-fatal; builds and the Helm repo step retry |
| FAILED marker missing from the log | `tee` was asynchronous, so the upload raced the final lines | Short sleep before the failure upload |
| Alerts firing but the engine got nothing | Rules using `sum by (service)` drop the `namespace` label, and Alertmanager only routes `namespace = "autosre"`, so they went to the `null` receiver | Every rule sets `namespace: autosre`; a test enforces it |
| Grafana UI intermittent, port-forward dropping | Grafana was OOM-killed (exit 137): the 256Mi limit is too small for ~30 dashboards | Limit raised to 768Mi |
| Port-forward gone after the pod restarted | `kubectl port-forward` exits when its pod does | Run it in a restart loop |
| Unhelpful *"Failed to fetch"* in Grafana | Same OOM crash-loop | As above |
| Memory-injection trial errored with `exit code 137` | `kubectl exec` runs its client *inside the target container's memory cgroup*; with the held memory just under the 128Mi limit, the injector's own overhead pushed the pod over and it was OOM-killed | Inject through the API server's service proxy (`kubectl create --raw .../proxy/...`), which runs nothing in the pod; aim for 0.925 of the limit |
| Trials slower than planned | `rate(...[5m])` keeps alerts firing ~5 minutes after a fault ends | Budget 8-9 minutes per trial |

### Patching a run in flight

Everything is reachable through Systems Manager, so a bug found mid-run can be fixed without
restarting the cluster: upload a new tarball, pull it onto the instance, and start the
orchestrator again with `--resume`. It keeps the completed trials, redoes errored ones, replaces
every pod so no earlier fault lingers, and numbers new trials after the existing ones. Stop the
old orchestrator *between* trials (right after a record lands in `results/trials.jsonl`), and
use a `15[0]`-style regex in `pkill -f` so the pattern does not match its own command line.

## Cleaning up

Terminate the instance (`aws ec2 terminate-instances --instance-ids <id>`) as soon as you
have the results; the lab's credit budget is shared with everything else in the account.
Empty and delete the bucket afterwards. The security group the wizard created is left
behind and can be deleted too.

## Getting results out

```bash
aws s3 ls $B/results/
```

`report.md` is the headline. To read it in a browser without downloading, copy it to a
`.txt` key with a `text/plain` content type, presign it for a few minutes, and open the URL.

Reading files from the console: a `.jsonl` object is served as a download, and browser
downloads from the S3 console can stall as `.tmp` files. Copy the file to a `.txt` key with
`--content-type text/plain` (in CloudShell), then **Open** it from the console and it renders
inline. Pages opened that way cannot call `localhost`, so there is no way to push a file to a
local receiver; read the text and write it into the repo.
