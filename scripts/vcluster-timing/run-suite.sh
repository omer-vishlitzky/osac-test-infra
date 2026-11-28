#!/bin/bash
# run-suite.sh RUN_NAME [SUITE_DIR]
#
# Runs an e2e suite against one vcluster run with all the vcluster-specific
# environment wired. The suite must run against the checkout that matches
# the run's installed code (/tmp/osac-impl).
set -euo pipefail

RUN_ARG="${1:?Usage: run-suite.sh RUN_NAME [SUITE_DIR]}"
SUITE="${2:-vmaas/sanity}"
DOMAIN="apps.test-infra-cluster-poc.redhat.com"
TI=/tmp/ti-impl
OSAC=/tmp/osac-impl
OUT=/tmp/opencode/poc/parallel-e2e
KC="${TI}/.vcluster-runs/${RUN_ARG}/kubeconfig-local"
mkdir -p "${OUT}"

# The venv is shared between concurrent suite runs: sync once here, then
# every run uses --no-sync so they cannot race on .venv creation.
cd "${OSAC}"
uv sync >/dev/null 2>&1

export KUBECONFIG="${KC}"
export OSAC_HUB_KUBECONFIG="${KC}"
export OSAC_VM_KUBECONFIG="$HOME/.kube/poc.kubeconfig"
export OSAC_NAMESPACE=osac
export OSAC_CLUSTER_DOMAIN="${DOMAIN}"
export OSAC_FULFILLMENT_ADDRESS="fulfillment-api-${RUN_ARG}.${DOMAIN}:443"
export OSAC_FULFILLMENT_PRIVATE_ADDRESS="fulfillment-internal-api-${RUN_ARG}.${DOMAIN}:443"
export OSAC_KEYCLOAK_URL="https://keycloak-keycloak.${DOMAIN}"
export OSAC_KEYCLOAK_REALM="${RUN_ARG}"
export OSAC_KEYCLOAK_CLIENT_ID=osac-cli
# The master-realm admin password comes from the outer cluster's secret:
# the suite's organization-membership fixture logs into the master realm
# with it. OSAC_JWT_PASSWORD is the realm users' password (foobar), which
# is a different thing.
KC_ADMIN_PASS="$(oc get secret -n keycloak keycloak-admin-credentials -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)"
export OSAC_KEYCLOAK_ADMIN_PASSWORD="${KC_ADMIN_PASS:-admin}"
export OSAC_JWT_PASSWORD=foobar
export OSAC_IMPERSONATE_USER=
export METERING_ADAPTER_URL="https://osac-metering-echo-adapter-${RUN_ARG}.${DOMAIN}"

exec uv run --no-sync pytest "tests/e2e/${SUITE}/" \
    --junitxml="${OUT}/${RUN_ARG}-junit.xml" \
    -q --tb=line
