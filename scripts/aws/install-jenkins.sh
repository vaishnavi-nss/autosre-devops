#!/usr/bin/env bash
# Run on the lab instance as root (for example through SSM). Installs Jenkins as a service on port 8080 that runs as the same user
# that owns kubectl/minikube/docker, with one pipeline job `autosre-cicd` built from the repository's Jenkinsfile.
# The admin password is generated here and stored in /home/$OWNER/.jenkins-admin-pass (owner-only); it is never printed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
OWNER="${OWNER:-ubuntu}"
REPO="${REPO:-https://github.com/vaishnavi-nss/autosre-devops}"
BRANCH="${BRANCH:-devops-completion}"
HOME_DIR=/var/lib/jenkins
PASSFILE="/home/$OWNER/.jenkins-admin-pass"

apt-get -o DPkg::Lock::Timeout=900 update -qq
apt-get -o DPkg::Lock::Timeout=900 install -y -qq openjdk-17-jre-headless fontconfig python3-venv git curl >/dev/null
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.jenkins.io/debian-stable/jenkins.io-2023.key -o /usr/share/keyrings/jenkins-keyring.asc
echo "deb [signed-by=/usr/share/keyrings/jenkins-keyring.asc] https://pkg.jenkins.io/debian-stable binary/" > /etc/apt/sources.list.d/jenkins.list
apt-get -o DPkg::Lock::Timeout=900 update -qq
apt-get -o DPkg::Lock::Timeout=900 install -y -qq jenkins >/dev/null
systemctl stop jenkins || true

# Plugins, installed before first start so no setup wizard is needed.
PM=/opt/jenkins-plugin-manager.jar
[ -f "$PM" ] || curl -fsSL -o "$PM" https://github.com/jenkinsci/plugin-installation-manager-tool/releases/download/2.13.2/jenkins-plugin-manager-2.13.2.jar
install -d "$HOME_DIR/plugins" "$HOME_DIR/init.groovy.d" "$HOME_DIR/jobs/autosre-cicd"
java -jar "$PM" --war /usr/share/java/jenkins.war --plugin-download-directory "$HOME_DIR/plugins" \
     --plugins workflow-aggregator git junit timestamper pipeline-stage-view >/dev/null

# Admin user from a generated password; anonymous users get nothing.
[ -f "$PASSFILE" ] || { head -c 18 /dev/urandom | base64 | tr -d '/+=' > "$PASSFILE"; chmod 600 "$PASSFILE"; chown "$OWNER:$OWNER" "$PASSFILE"; }
cat > "$HOME_DIR/init.groovy.d/security.groovy" <<EOF
import jenkins.model.*
import hudson.security.*
def j = Jenkins.get()
def realm = new HudsonPrivateSecurityRealm(false)
realm.createAccount('admin', new File('$PASSFILE').text.trim())
j.setSecurityRealm(realm)
def auth = new FullControlOnceLoggedInAuthorizationStrategy()
auth.setAllowAnonymousRead(false)
j.setAuthorizationStrategy(auth)
j.save()
EOF

cat > "$HOME_DIR/jobs/autosre-cicd/config.xml" <<EOF
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <description>Test, build the seven AutoSRE images into Minikube, deploy to the autosre namespace and verify. Leaves AUTO_REMEDIATE=false.</description>
  <keepDependencies>false</keepDependencies>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs><hudson.plugins.git.UserRemoteConfig><url>$REPO</url></hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>
      <branches><hudson.plugins.git.BranchSpec><name>*/$BRANCH</name></hudson.plugins.git.BranchSpec></branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions/>
    </scm>
    <scriptPath>Jenkinsfile</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
EOF

# Run as the user that owns kubectl, minikube and docker; keep the JVM small (the same 8 GB host also runs Minikube).
install -d /etc/systemd/system/jenkins.service.d
cat > /etc/systemd/system/jenkins.service.d/override.conf <<EOF
[Service]
User=$OWNER
Group=$OWNER
Environment="JAVA_OPTS=-Djava.awt.headless=true -Djenkins.install.runSetupWizard=false -Xmx768m"
Environment="KUBECONFIG=/home/$OWNER/.kube/config"
Environment="MINIKUBE_HOME=/home/$OWNER"
EOF
chown -R "$OWNER:$OWNER" "$HOME_DIR" /var/cache/jenkins /var/log/jenkins 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now jenkins
for _ in $(seq 1 40); do curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/login 2>/dev/null | grep -q 200 && break; sleep 5; done
printf "jenkins on :8080 -> HTTP "; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/login
echo "login: admin / (password in $PASSFILE)"
