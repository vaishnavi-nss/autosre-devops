#!/usr/bin/env bash
# Run this in AWS CloudShell after signing in to the lab through its own "AWS" link. It starts the lab instance if stopped, waits
# for Systems Manager, then launches scripts/aws/completion-run.sh on it. Progress is written to the SSM parameter `autosre-go`.
#
#   curl -fsS https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/devops-completion/scripts/aws/start-completion-run.sh | RUN=comp1 LIVE=0 bash
#
# Tunables: RUN, LIVE (0 decisions only, 1 live execution - needs approval), SCENARIOS, INSTANCE, BRANCH.
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INSTANCE="${INSTANCE:-i-04f8f4d75378a1aba}"
RUN="${RUN:-comp1}"; LIVE="${LIVE:-0}"; BRANCH="${BRANCH:-devops-completion}"
SCENARIOS="${SCENARIOS:-guardrails_dry,pool,memory,disk,unknown}"
COOLDOWN="${COOLDOWN:-900}"
COLLECT="${COLLECT:-0}"; SCORER_MODEL="${SCORER_MODEL:-}"
RAW="https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/$BRANCH/scripts/aws/completion-run.sh"

say() { echo "$(date -u +%H:%M:%S) $1"; aws ssm put-parameter --name autosre-go --type String --overwrite --value "$(date -u +%H:%M) $1" >/dev/null 2>&1 || true; }
state() { aws ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].State.Name' --output text; }
ping_status() { aws ssm describe-instance-information --query 'InstanceInformationList[].PingStatus' --output text; }

S=$(state); say "instance is $S"
if [ "$S" = stopped ]; then aws ec2 start-instances --instance-ids "$INSTANCE" >/dev/null && say "start requested"; fi
for _ in $(seq 1 60); do
  S=$(state); P=$(ping_status)
  [ "$S" = running ] && [ "$P" = Online ] && break
  say "waiting: $S / ssm ${P:-none}"; sleep 20
done
if [ "$S" != running ] || [ "$P" != Online ]; then say "gave up: $S / ssm ${P:-none}"; exit 1; fi
sleep 30
CMD="curl -fsS $RAW -o /tmp/completion-run.sh && RUN=$RUN LIVE=$LIVE BRANCH=$BRANCH SCENARIOS=$SCENARIOS COOLDOWN=$COOLDOWN COLLECT=$COLLECT SCORER_MODEL=$SCORER_MODEL setsid nohup bash /tmp/completion-run.sh >/tmp/completion-run.out 2>&1 < /dev/null &"
ID=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
       --parameters "commands=[\"$CMD\"]" --query Command.CommandId --output text)
say "$RUN launch command sent ($ID); watch progress in the parameter autosre-prep and the log in s3 results-$RUN"
