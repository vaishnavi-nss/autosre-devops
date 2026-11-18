#!/usr/bin/env bash
# Run on the lab instance as root (for example through SSM). Makes the demo reachable from your browser:
#
#   8081 AutoSRE dashboard   3000 Grafana   9090 Prometheus   8082 fault console   (8080 Jenkins, see install-jenkins.sh)
#
# Each is a systemd service that restarts itself. Nothing is reachable from outside until YOU add a security-group rule for the
# port from your own IP (never 0.0.0.0/0: these pages have no login of their own except the fault console's token).
# Delete the rules when you are done.
set -euo pipefail
OWNER="${OWNER:-ubuntu}"
REPO="${REPO:-https://github.com/vaishnavi-nss/autosre-devops}"
BRANCH="${BRANCH:-devops-completion}"
CONSOLE_DIR="/home/$OWNER/autosre-console"

pkill -f 'kubectl.*port-forward' 2>/dev/null || true       # the hand-started forwards from earlier

# The fault console runs from its own checkout so that a rebuild of the lab run does not remove it.
rm -rf "$CONSOLE_DIR"
git clone -q --depth 1 -b "$BRANCH" "$REPO" "$CONSOLE_DIR"
chown -R "$OWNER:$OWNER" "$CONSOLE_DIR"

unit() {   # name, description, command, extra env line
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target

[Service]
User=$OWNER
Environment=KUBECONFIG=/home/$OWNER/.kube/config
$4
ExecStart=$3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}
KC="$(command -v kubectl)"
unit autosre-pf-frontend   "AutoSRE dashboard on :8081"  "$KC -n autosre port-forward --address 0.0.0.0 svc/frontend 8081:8000" ""
unit autosre-pf-grafana    "Grafana on :3000"            "$KC -n monitoring port-forward --address 0.0.0.0 svc/monitoring-grafana 3000:80" ""
unit autosre-pf-prometheus "Prometheus on :9090"         "$KC -n monitoring port-forward --address 0.0.0.0 svc/monitoring-kube-prometheus-prometheus 9090:9090" ""
unit autosre-fault-console "AutoSRE fault console on :8082" \
     "/usr/bin/python3 $CONSOLE_DIR/scripts/fault_console/console.py --port 8082 --repo /home/$OWNER/autosre-completion" \
     "Environment=FAULT_CONSOLE_ALLOW_LIVE=1"
systemctl daemon-reload
for s in autosre-pf-frontend autosre-pf-grafana autosre-pf-prometheus autosre-fault-console; do systemctl enable --now "$s"; done
sleep 6
for p in 8081 3000 9090 8082; do printf "port %s -> HTTP " "$p"; curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:$p/" || true; done
echo "fault console token is in /home/$OWNER/.fault-console-token (owner-only file)"
