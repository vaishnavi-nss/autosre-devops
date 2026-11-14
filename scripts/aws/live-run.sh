#!/usr/bin/env bash
# Run the live validation on the lab instance from the current branch, as root (for example through SSM).
#
#   RUN=run11 PER_CAUSE=3 UNKNOWN=5 DEADLINE=150 bash live-run.sh [extra live_validate.py flags, e.g. --stale]
#
# What it does: copies the WHOLE correlation-engine/ and second-opinion/ directories from the branch (a file list
# let correlate.py fall out of step with app.py in run 10, and the engine answered 500 to every webhook),
# rebuilds both images, replaces every pod so no stale alert or fault is left, then starts the harness. The harness
# itself checks that the engine accepts a webhook before the first trial and stops if it does not.
# Progress goes to the SSM parameter `autosre-prep`; results sync to s3://$BUCKET/results-$RUN every 45 s.
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
BUCKET="${BUCKET:-autosre-validation-841736140404}"
REPO="${REPO:-https://github.com/vaishnavi-nss/autosre-devops}"
BRANCH="${BRANCH:-ml-and-cloud-validation}"
RUN="${RUN:?set RUN, e.g. run11}"
PER_CAUSE="${PER_CAUSE:-3}"; UNKNOWN="${UNKNOWN:-5}"; DEADLINE="${DEADLINE:-150}"
HOME_DIR=/home/ubuntu

st() { aws ssm put-parameter --name autosre-prep --type String --overwrite \
         --value "$(date -u +%H:%M) $1" >/dev/null 2>&1 || true; }

st "$RUN: fetching $BRANCH"
pkill -f 'live_validat[e]' || true
rm -rf /tmp/src && git clone -q --depth 1 -b "$BRANCH" "$REPO" /tmp/src || { st "$RUN: git clone failed"; exit 1; }
SRC=/tmp/src/autosre
[ -d "$SRC" ] || SRC=/tmp/src
for d in correlation-engine second-opinion; do
  mkdir -p "$HOME_DIR/autosre/$d" && cp -a "$SRC/$d/." "$HOME_DIR/autosre/$d/" || { st "$RUN: copying $d failed"; exit 1; }
done
chown -R ubuntu:ubuntu "$HOME_DIR/autosre"
COMMIT=$(git -C /tmp/src rev-parse --short HEAD)
MODEL=$(md5sum "$HOME_DIR/autosre/second-opinion/models/own.joblib" | cut -c1-8)
st "$RUN: commit $COMMIT, model $MODEL; building"

cat > "$HOME_DIR/prep-live.sh" <<'EOF'
#!/bin/bash
minikube status >/dev/null 2>&1 || minikube start --driver=docker
eval "$(minikube docker-env)"
cd /home/ubuntu/autosre
docker build -q -t autosre/second-opinion:dev second-opinion >/dev/null
docker build -q -t autosre/correlation-engine:dev correlation-engine >/dev/null
kubectl -n autosre delete pods --all --wait=false
kubectl -n monitoring delete pods --all --wait=false
sleep 60
kubectl -n monitoring wait --for=condition=Ready pods --all --timeout=420s 2>&1 | tail -1
kubectl -n autosre wait --for=condition=Ready pods --all --timeout=300s 2>&1 | tail -1
EOF
chmod +x "$HOME_DIR/prep-live.sh"; chown ubuntu:ubuntu "$HOME_DIR/prep-live.sh"
OUT=$(sudo -u ubuntu -H -g docker bash "$HOME_DIR/prep-live.sh" 2>&1 | tail -4 | tr '\n' '|' | cut -c1-400)
st "$RUN: pods up ($OUT); settling 4 min"
sleep 240

mkdir -p "$HOME_DIR/results/$RUN" && chown ubuntu:ubuntu "$HOME_DIR/results/$RUN"
setsid nohup bash -c "while true; do aws s3 sync $HOME_DIR/results/$RUN s3://$BUCKET/results-$RUN --only-show-errors; \
  aws s3 cp $HOME_DIR/$RUN.log s3://$BUCKET/results-$RUN/$RUN.log --only-show-errors; sleep 45; done" >/dev/null 2>&1 </dev/null &
# HARNESS=real_aws_validate.py runs the REAL-failure driver instead (no chaos endpoints), with PLAN="healthy:15,memory_pressure:3".
if [ "${HARNESS:-live_validate.py}" = "real_aws_validate.py" ]; then
  sudo -u ubuntu -H -g docker bash -c "cd $HOME_DIR/autosre/correlation-engine && setsid nohup python3 evaluation/real_aws_validate.py \
    --out $HOME_DIR/results/$RUN --plan '${PLAN:?set PLAN, e.g. healthy:15,memory_pressure:3}' --deadline-minutes $DEADLINE $* \
    > $HOME_DIR/$RUN.log 2>&1 < /dev/null &"
else
  sudo -u ubuntu -H -g docker bash -c "cd $HOME_DIR/autosre/correlation-engine && setsid nohup python3 evaluation/live_validate.py \
    --out $HOME_DIR/results/$RUN --per-cause $PER_CAUSE --unknown $UNKNOWN --deadline-minutes $DEADLINE $* \
    > $HOME_DIR/$RUN.log 2>&1 < /dev/null &"
fi
sleep 5
st "$RUN: harness started on $COMMIT (model $MODEL): $(pgrep -af 'live_validat[e]' | head -1 | cut -c1-60)"
