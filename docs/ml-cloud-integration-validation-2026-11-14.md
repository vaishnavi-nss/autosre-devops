# AutoSRE ML + Cloud Integration — November 14, 2026

## Tested deployment and image

Branch: integration/ml-cloud-final

| Component | Image ID |
|---|---|
| Correlation Engine | sha256:2336047d6a3638a013535bfddb6d0824746cb78441d88e98c0b815e4cac697a1 |
| ML Second Opinion | sha256:6ae6de9d09a120e6e5c9b9a193c7845d4a76a3f9f31cb79c7e718b02e562c395 |
| Remediation Dispatcher | sha256:676c015a0a810e09aaf069992ce3ef252b4b4e1be9c38c2c9bcf090860dccd97 |
| Transaction Service | sha256:502fbc87e8756ececf971bf4735fb9459484a9a0765bb64d493fac95d4474175 |

## Exact tested build procedure

The correlation-engine Docker image was rebuilt, loaded into Minikube, and its deployment restarted successfully.

All seven AutoSRE application pods were Ready before and after testing.

## Safety preflight and synthetic procedure

Verified settings:

- AUTO_REMEDIATE=false
- CHAOS_ENABLED=false
- SECOND_OPINION_MODE=shadow
- No active AutoSRE alerts before testing
- No active Alertmanager silences

Four synthetic warning alerts were submitted through Alertmanager:

1. DBPoolNearExhaustion
2. ErrorRateHigh
3. PodRestartsHigh
4. APILatencyHigh

Test ID: autosre-test-20261114132835

Alertmanager accepted the submission with HTTP 200. After 75 seconds, resolution notifications were submitted successfully.

## Verified result and Prometheus queries

Correlation engine received all four alerts and identified:

- Root cause: db_connection_pool_exhaustion
- Service: transaction-service
- Rule-based confidence: 100%
- Symptoms matched: 4/4

ML second opinion returned disagree because live metrics pointed toward api-gateway while synthetic alerts identified transaction-service.

Remediation dispatcher returned HTTP 200 with:

- Status: dry_run
- Playbook: restart_service.yml
- Target: transaction-service

No Ansible playbook was executed.

Correlation engine received the resolved notification for all four alerts.

Final checks:

- Remaining synthetic alerts: 0
- Application pods: 7/7 Running and Ready
- New pod restarts: 0

## Rollback and limits

Automatic remediation remained disabled throughout the test.

The test used synthetic Alertmanager alerts rather than real infrastructure failures. It verifies integration and dry-run decision handling, not successful execution of remediation playbooks.

The ML second-opinion disagreement is expected to be investigated separately if agreement under realistic incident metrics is required.

Previous November 12 validation evidence remains unchanged.
