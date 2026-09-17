# Environment setup — WSL2 Ubuntu

Everything in AutoSRE runs inside **WSL2 Ubuntu**. The reason is Ansible: it cannot act as
a control node on native Windows, so the control node has to be Linux. Keeping Docker,
minikube, kubectl, Helm and Ansible in the same Linux environment also means one kubeconfig
and no Windows/WSL path translation.

Run every command in this guide **inside Ubuntu** unless it says PowerShell.

---

## 1. Install WSL2 and Ubuntu

In **PowerShell as Administrator**, on the Windows host:

```powershell
wsl --install -d Ubuntu
```

Reboot if prompted, then open Ubuntu and create your Linux username and password.

Check it is version 2:

```powershell
wsl -l -v
```

### Give WSL enough memory

minikube gets 6 GB, and WSL defaults to about half the host's RAM. On a 16 GB machine that
is tight once Prometheus and Grafana are running. Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=10GB
processors=4
```

Then `wsl --shutdown` in PowerShell and reopen Ubuntu.

## 2. Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git ca-certificates python3-pip python3-venv pipx
pipx ensurepath
```

## 3. Docker Engine

Docker Desktop is not needed. Install Docker Engine directly inside Ubuntu:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# Run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

sudo service docker start
docker run --rm hello-world
```

> Docker does not start automatically in WSL. Run `sudo service docker start` after each
> Windows restart, or add it to your `~/.bashrc`.

## 4. kubectl, minikube and Helm

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# minikube
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64

# Helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

kubectl version --client
minikube version
helm version
```

Start the cluster:

```bash
minikube start --driver=docker --cpus=4 --memory=6g
kubectl get nodes
```

## 5. Ansible

```bash
pipx install --include-deps ansible-core
pipx inject ansible-core kubernetes    # Python client required by kubernetes.core

cd ~/autosre
ansible-galaxy collection install -r ansible/requirements.yml

ansible --version
```

## 6. Clone the repo

Clone into your **Linux home directory**, not `/mnt/c`. Files on `/mnt/c` cross the
Windows filesystem boundary, which makes Docker builds and file watching several times
slower.

```bash
cd ~
git clone https://github.com/<your-username>/autosre.git
cd autosre
```

Editing from Windows still works — VS Code with the WSL extension, or `\\wsl$\Ubuntu\home\<you>\autosre` in Explorer.

## 7. First run

```bash
# Monitoring stack
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f monitoring/kube-prometheus-stack-values.yaml

# Application
./scripts/build-images.sh
./scripts/deploy.sh
./scripts/smoke-test.sh
```

## 8. Verify what could not be checked on Windows

The scaffold was written and tested on Windows, where Docker, Kubernetes and Ansible were
not installed. These are the first commands to run once this environment exists:

```bash
# Images build
./scripts/build-images.sh

# Manifests are accepted by the API server
kubectl apply --dry-run=server -f k8s/

# Playbooks parse
cd ansible
ansible-playbook --syntax-check playbooks/restart_service.yml
ansible-playbook --syntax-check playbooks/scale_replicas.yml
ansible-playbook --syntax-check playbooks/clear_logs.yml

# Prometheus rules are valid
kubectl apply --dry-run=server -f monitoring/alert-rules.yaml
```

## Reaching the dashboards

```bash
minikube service frontend -n autosre                                    # the app
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80       # Grafana
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-alertmanager 9093:9093
```

Grafana's generated admin password:

```bash
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath="{.data.admin-password}" | base64 -d; echo
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Cannot connect to the Docker daemon` | `sudo service docker start` — it does not auto-start in WSL. |
| `/bin/bash^M: bad interpreter` | A script picked up CRLF line endings. `.gitattributes` forces LF; re-clone or run `dos2unix`. |
| Pods stuck `ImagePullBackOff` | Images were built outside minikube's Docker. Re-run `./scripts/build-images.sh`, which does `eval $(minikube docker-env)` first. |
| Prometheus shows no AutoSRE targets | The `serviceMonitorSelectorNilUsesHelmValues: false` value did not apply. Re-run the `helm upgrade`. |
| Alerts fire but nothing reaches the engine | Check the Alertmanager route in the Helm values, and that `correlation-engine` is running in the `autosre` namespace. |
| minikube fails to start, low memory | Lower to `--memory=4g`, or raise the `memory=` value in `.wslconfig`. |
