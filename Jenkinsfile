// AutoSRE CI/CD on the lab instance: test -> build the seven images into Minikube -> deploy -> verify.
// Runs on the instance itself (it has kubectl, minikube and docker). It deploys to the isolated `autosre` namespace only and
// ALWAYS leaves the dispatcher in its safe default (AUTO_REMEDIATE=false); it refuses to run while a fault scenario is in progress.
pipeline {
    agent any
    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 40, unit: 'MINUTES')
    }
    parameters {
        booleanParam(name: 'RUN_TESTS', defaultValue: true, description: 'Run the unit tests before building')
    }
    environment {
        NS = 'autosre'
    }
    stages {
        stage('Preflight') {
            steps {
                sh '''
                    set -eu
                    minikube status >/dev/null
                    if pgrep -f 'run_scenario[s]' >/dev/null; then echo "a fault scenario is running; not deploying under it"; exit 1; fi
                    A=$(kubectl -n $NS get deploy remediation-dispatcher -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='AUTO_REMEDIATE')].value}")
                    echo "AUTO_REMEDIATE is currently: ${A:-unset}"
                    [ "${A:-false}" = "false" ] || { echo "AUTO_REMEDIATE is not false; refusing to deploy"; exit 1; }
                '''
            }
        }
        stage('Unit tests') {
            when { expression { params.RUN_TESTS } }
            steps {
                sh '''
                    set -eu
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install -q --upgrade pip
                    pip install -q -r correlation-engine/requirements-dev.txt -r remediation-dispatcher/requirements.txt \
                        -r second-opinion/requirements.txt -r services/frontend/requirements.txt -r services/api-gateway/requirements.txt \
                        -r services/database/requirements.txt -r services/transaction_service/requirements.txt
                    python -m pytest -q --junitxml=test-results.xml tests correlation-engine/tests second-opinion/tests
                '''
            }
            post { always { junit allowEmptyResults: true, testResults: 'test-results.xml' } }
        }
        stage('Build images') {
            steps { sh 'bash scripts/build-images.sh' }
        }
        stage('Deploy') {
            steps {
                sh '''
                    set -eu
                    kubectl apply -f k8s/namespace.yaml
                    kubectl apply -f k8s/
                    # Force the safe defaults, exactly as the lab launcher does, so a deploy can never leave live remediation on.
                    kubectl -n $NS set env deploy/remediation-dispatcher AUTO_REMEDIATE=false COOLDOWN_SECONDS- VERIFY_TIMEOUT_SECONDS-
                    kubectl -n $NS set env deploy/transaction-service CHAOS_ENABLED=false
                    kubectl -n $NS set env deploy/database CHAOS_ENABLED=false
                    for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher; do
                        kubectl -n $NS rollout restart deploy/$d
                    done
                '''
            }
        }
        stage('Verify') {
            steps {
                sh '''
                    set -eu
                    for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher loadgen; do
                        kubectl -n $NS rollout status deploy/$d --timeout=300s
                    done
                    A=$(kubectl -n $NS get deploy remediation-dispatcher -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='AUTO_REMEDIATE')].value}")
                    [ "$A" = "false" ] || { echo "AUTO_REMEDIATE is '$A', expected false"; exit 1; }
                    bash scripts/smoke-test.sh
                '''
            }
        }
    }
    post {
        always { sh 'kubectl -n $NS get deploy --no-headers || true' }
        failure { echo 'The deploy did not verify. Nothing was left in a live-remediation state (AUTO_REMEDIATE is only ever set to false here).' }
    }
}
