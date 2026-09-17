#!/usr/bin/env bash
set -euo pipefail

TRIGGER_LABEL="${TRIGGER_LABEL:-e2e-ready}"
PR_NUMBER="${PR_NUMBER:?PR_NUMBER is required}"
REPO="${REPO:?REPO is required}"
EVENT_HEAD_SHA="${EVENT_HEAD_SHA:-}"
WORKFLOWS="${WORKFLOWS:-e2e-vmaas-full-install-caller.yml,e2e-bmaas-full-install-caller.yml,e2e-caas-full-install-caller.yml}"

if [[ "${TRIGGER_LABEL}" != "e2e-ready" ]]; then
  echo "Unsupported unlock label: ${TRIGGER_LABEL}" >&2
  exit 1
fi

pr_json=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}")
if [[ "$(jq -r '.state' <<<"${pr_json}")" != "open" ]]; then
  echo "PR #${PR_NUMBER} is not open; skipping."
  exit 0
fi

head_sha=$(jq -r '.head.sha' <<<"${pr_json}")
if [[ -n "${EVENT_HEAD_SHA}" && "${head_sha}" != "${EVENT_HEAD_SHA}" ]]; then
  echo "PR head moved (${EVENT_HEAD_SHA:0:7} -> ${head_sha:0:7}); refusing to start stale E2E."
  exit 0
fi

labels_json=$(gh api --paginate --slurp "repos/${REPO}/issues/${PR_NUMBER}/labels?per_page=100" | jq 'add')
if ! jq -e '[.[].name] | index("e2e-ready") != null' <<<"${labels_json}" >/dev/null; then
  echo "e2e-ready is not present on PR #${PR_NUMBER}; skipping."
  exit 0
fi

events_json=$(gh api --paginate --slurp "repos/${REPO}/issues/${PR_NUMBER}/events?per_page=100" | jq 'add')
if ! jq -e '
  [.[]
    | select(.event == "labeled")
    | select(.label.name == "e2e-ready")
  ] | last | .actor.login == "github-actions[bot]"
' <<<"${events_json}" >/dev/null; then
  echo "e2e-ready was not applied by the trusted command handler; skipping."
  exit 0
fi

merge_ref="refs/pull/${PR_NUMBER}/merge"
workflow_ref="main"
if [[ "${REPO}" == "osac-project/osac" ]]; then
  source_repository="${REPO}"
  source_ref="${merge_ref}"
  test_repository="${REPO}"
  test_ref="${merge_ref}"
  installer_repository="${REPO}"
  installer_ref="${merge_ref}"
  test_infra_ref="main"
elif [[ "${REPO}" == "osac-project/osac-test-infra" ]]; then
  source_repository="${REPO}"
  source_ref="${merge_ref}"
  test_repository="osac-project/osac"
  test_ref="main"
  installer_repository="osac-project/osac"
  installer_ref="main"
  test_infra_ref="${merge_ref}"
else
  echo "Unsupported repository: ${REPO}" >&2
  exit 1
fi

started=0
IFS=',' read -r -a workflow_list <<<"${WORKFLOWS}"
for workflow in "${workflow_list[@]}"; do
  workflow="${workflow#"${workflow%%[![:space:]]*}"}"
  workflow="${workflow%"${workflow##*[![:space:]]}"}"
  [[ -n "${workflow}" ]] || continue

  echo "Dispatching ${workflow} from ${workflow_ref} with source ref ${merge_ref}."
  gh workflow run "${workflow}" \
    --repo "${REPO}" \
    --ref "${workflow_ref}" \
    -f "pr-number=${PR_NUMBER}" \
    -f "head-sha=${head_sha}" \
    -f "installer-repo=${installer_repository}" \
    -f "installer-ref=${installer_ref}" \
    -f "source-repository=${source_repository}" \
    -f "source-ref=${source_ref}" \
    -f "test-repository=${test_repository}" \
    -f "test-ref=${test_ref}" \
    -f "test-infra-ref=${test_infra_ref}"
  started=$((started + 1))
done

if [[ "${started}" -eq 0 ]]; then
  echo "No E2E workflows were dispatched." >&2
  exit 1
fi

body="Started ${started} fresh E2E workflow(s) for PR merge ref \`${merge_ref}\`."
gh pr comment "${PR_NUMBER}" --repo "${REPO}" --body "${body}"
