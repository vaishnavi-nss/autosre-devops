#!/usr/bin/env bash
# Run on the lab instance as root. Installs a systemd timer that copies AutoSRE's history (incidents, events, playbook runs, run logs) to the
# S3 bucket every 2 minutes, because when the lab restarts only the bucket survives. bring-up.sh pulls it back.
#   BUCKET=autosre-validation-841736140404 bash install-history-sync.sh
set -euo pipefail
OWNER="${OWNER:-ubuntu}"
BUCKET="${BUCKET:-autosre-validation-841736140404}"
SCRIPT="${SCRIPT:-/home/$OWNER/autosre-console/scripts/aws/history_sync.py}"

cat > /etc/systemd/system/autosre-history-sync.service <<UNIT
[Unit]
Description=Copy AutoSRE history to the S3 bucket
After=network-online.target

[Service]
Type=oneshot
User=$OWNER
Environment=KUBECONFIG=/home/$OWNER/.kube/config
Environment=BUCKET=$BUCKET
ExecStart=/usr/bin/python3 $SCRIPT push
UNIT

cat > /etc/systemd/system/autosre-history-sync.timer <<UNIT
[Unit]
Description=Copy AutoSRE history to the S3 bucket every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Unit=autosre-history-sync.service

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now autosre-history-sync.timer
systemctl list-timers autosre-history-sync.timer --no-pager | head -3
