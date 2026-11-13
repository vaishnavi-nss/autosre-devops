#!/usr/bin/env bash
# Run this in AWS CloudShell. It starts the lab instance if it is stopped, waits until Systems Manager can reach
# it, then launches scripts/aws/live-run.sh on it. Progress is written to the SSM parameter `autosre-go`.
#
#   curl -fsS https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/ml-and-cloud-validation/scripts/aws/start-live-run.sh | RUN=run11 bash
#
# Tunables (environment): RUN, PER_CAUSE, UNKNOWN, DEADLINE, INSTANCE, BRANCH, EXTRA (e.g. EXTRA=--stale).
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INSTANCE="${INSTANCE:-i-04f8f4d75378a1aba}"
RUN="${RUN:-run11}"; PER_CAUSE="${PER_CAUSE:-3}"; UNKNOWN="${UNKNOWN:-5}"; DEADLINE="${DEADLINE:-150}"
BRANCH="${BRANCH:-ml-and-cloud-validation}"; EXTRA="${EXTRA:-}"
RAW="https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/$BRANCH/scripts/aws/live-run.sh"

say() { echo "$(date -u +%H:%M:%S) $1"; aws ssm put-parameter --name autosre-go --type String --overwrite \
          --value "$(date -u +%H:%M) $1" >/dev/null 2>&1 || true; }
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
HARNESS="${HARNESS:-live_validate.py}"; PLAN="${PLAN:-}"      # HARNESS=real_aws_validate.py PLAN=healthy:15,memory_pressure:3
CMD="curl -fsS $RAW -o /tmp/live-run.sh && RUN=$RUN PER_CAUSE=$PER_CAUSE UNKNOWN=$UNKNOWN DEADLINE=$DEADLINE HARNESS=$HARNESS PLAN=$PLAN setsid nohup bash /tmp/live-run.sh $EXTRA >/tmp/live-run.out 2>&1 < /dev/null &"
ID=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
       --parameters "commands=[\"$CMD\"]" --query Command.CommandId --output text)
say "$RUN launch command sent ($ID); watch progress in the parameter autosre-prep"
