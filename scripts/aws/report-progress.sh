#!/usr/bin/env bash
# Run this in AWS CloudShell. It starts a small loop ON THE INSTANCE that copies the tail of the live run's log into
# the SSM parameter `autosre-live` every 2 minutes, so the run can be followed from the console
# (Systems Manager > Parameter Store > autosre-live) without a terminal.
#
#   curl -fsS https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/ml-and-cloud-validation/scripts/aws/report-progress.sh | RUN=run11 bash
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INSTANCE="${INSTANCE:-i-04f8f4d75378a1aba}"
RUN="${RUN:-run11}"
LOOP="pkill -f report-loop-$RUN || true; setsid nohup bash -c 'exec -a report-loop-$RUN bash -c \"while true; do aws ssm put-parameter --region us-east-1 --name autosre-live --type String --overwrite --value \\\"\$(date -u +%H:%M) \$(grep -v quiet /home/ubuntu/$RUN.log | tail -n 14 | cut -c1-200)\\\" >/dev/null 2>&1; sleep 120; done\"' >/dev/null 2>&1 </dev/null &"
ID=$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
       --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]]}))' "$LOOP")" \
       --query Command.CommandId --output text)
echo "progress reporter requested ($ID). Read it at Systems Manager > Parameter Store > autosre-live in about 2 minutes."
echo "--- the log right now:"
aws s3 cp "s3://autosre-validation-841736140404/results-$RUN/$RUN.log" - 2>&1 | grep -v 'waiting for quiet' | tail -n 20 | cut -c1-170
