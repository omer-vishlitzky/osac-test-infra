#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-osac-e2e-ci}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/tmp/osac-artifacts}"

mkdir -p "${ARTIFACT_DIR}"

gather() {
    local desc="$1"; shift
    echo "  ${desc}..."
    "$@" || true
}

echo "=== Gathering OSAC logs from namespace ${NAMESPACE} ==="

gather "pods" oc get pods -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/pods.txt" 2>&1
gather "events" oc get events -n "${NAMESPACE}" --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/events.txt" 2>&1
gather "pod descriptions" oc describe pods -n "${NAMESPACE}" > "${ARTIFACT_DIR}/pods-describe.txt" 2>&1
gather "deployments" oc get deployments -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/deployments.txt" 2>&1
gather "jobs" oc get jobs -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/jobs.txt" 2>&1
gather "statefulsets" oc get statefulsets -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/statefulsets.txt" 2>&1

echo "=== Collecting pod logs ==="
for pod in $(oc get pods -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    for container in $(oc get pod "${pod}" -n "${NAMESPACE}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null); do
        oc logs "${pod}" -n "${NAMESPACE}" -c "${container}" > "${ARTIFACT_DIR}/pod-${pod}-${container}.log" 2>&1 || true
        oc logs "${pod}" -n "${NAMESPACE}" -c "${container}" --previous > "${ARTIFACT_DIR}/pod-${pod}-${container}-previous.log" 2>/dev/null || true
    done
    for container in $(oc get pod "${pod}" -n "${NAMESPACE}" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null); do
        oc logs "${pod}" -n "${NAMESPACE}" -c "${container}" > "${ARTIFACT_DIR}/pod-${pod}-init-${container}.log" 2>&1 || true
    done
done

echo "=== Collecting auxiliary namespace logs ==="
for ns in keycloak ansible-aap; do
    if oc get namespace "${ns}" &>/dev/null; then
        mkdir -p "${ARTIFACT_DIR}/${ns}"
        gather "${ns} pods" oc get pods -n "${ns}" -o wide > "${ARTIFACT_DIR}/${ns}/pods.txt" 2>&1
        gather "${ns} events" oc get events -n "${ns}" --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/${ns}/events.txt" 2>&1
        gather "${ns} pod descriptions" oc describe pods -n "${ns}" > "${ARTIFACT_DIR}/${ns}/pods-describe.txt" 2>&1
        gather "${ns} deployments" oc get deployments -n "${ns}" -o wide > "${ARTIFACT_DIR}/${ns}/deployments.txt" 2>&1
        gather "${ns} jobs" oc get jobs -n "${ns}" -o wide > "${ARTIFACT_DIR}/${ns}/jobs.txt" 2>&1
        gather "${ns} statefulsets" oc get statefulsets -n "${ns}" -o wide > "${ARTIFACT_DIR}/${ns}/statefulsets.txt" 2>&1
        for pod in $(oc get pods -n "${ns}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            for container in $(oc get pod "${pod}" -n "${ns}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null); do
                oc logs "${pod}" -n "${ns}" -c "${container}" > "${ARTIFACT_DIR}/${ns}/pod-${pod}-${container}.log" 2>&1 || true
                oc logs "${pod}" -n "${ns}" -c "${container}" --previous > "${ARTIFACT_DIR}/${ns}/pod-${pod}-${container}-previous.log" 2>/dev/null || true
            done
            for container in $(oc get pod "${pod}" -n "${ns}" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null); do
                oc logs "${pod}" -n "${ns}" -c "${container}" > "${ARTIFACT_DIR}/${ns}/pod-${pod}-init-${container}.log" 2>&1 || true
            done
        done
    fi
done

echo "=== Collecting CNV/virtualization diagnostics ==="
mkdir -p "${ARTIFACT_DIR}/cnv"
gather "hyperconverged" oc get hyperconverged -A -o yaml > "${ARTIFACT_DIR}/cnv/hyperconverged.yaml" 2>&1
gather "VMs" oc get vms -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/vms.txt" 2>&1
gather "VMIs" oc get vmis -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/vmis.txt" 2>&1
gather "datavolumes" oc get datavolumes -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/datavolumes.txt" 2>&1
gather "PVCs" oc get pvc -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/cnv/pvcs.txt" 2>&1
gather "CNV events" oc get events -n openshift-cnv --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/cnv/events-openshift-cnv.txt" 2>&1

VM_NAMESPACES=$(oc get computeinstances -n "${NAMESPACE}" \
    -o jsonpath='{.items[*].status.virtualMachineReference.namespace}' 2>/dev/null | tr ' ' '\n' | sort -u)
for ns in ${VM_NAMESPACES}; do
    [[ -z "${ns}" || "${ns}" == "${NAMESPACE}" ]] && continue
    echo "  Gathering VM diagnostics from subnet namespace ${ns}..."
    mkdir -p "${ARTIFACT_DIR}/cnv/${ns}"
    oc get vms -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/vms.txt" 2>&1 || true
    oc get vms -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/vms.yaml" 2>&1 || true
    oc get vmis -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/vmis.txt" 2>&1 || true
    oc get datavolumes -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/datavolumes.txt" 2>&1 || true
    oc get datavolumes -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/datavolumes.yaml" 2>&1 || true
    oc get pvc -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/pvcs.txt" 2>&1 || true
    oc get events -n "${ns}" --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/cnv/${ns}/events.txt" 2>&1 || true
    oc get networkpolicies -n "${ns}" -o yaml > "${ARTIFACT_DIR}/cnv/${ns}/networkpolicies.yaml" 2>&1 || true
    oc get pods -n "${ns}" -o wide > "${ARTIFACT_DIR}/cnv/${ns}/pods.txt" 2>&1 || true
    for pod in $(oc get pods -n "${ns}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        oc logs "${pod}" -n "${ns}" --all-containers > "${ARTIFACT_DIR}/cnv/${ns}/pod-${pod}.log" 2>&1 || true
    done
done

echo "=== Collecting compute instance status ==="
gather "compute instances" oc get computeinstances -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/computeinstances.txt" 2>&1
gather "compute instances yaml" oc get computeinstances -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/computeinstances.yaml" 2>&1

echo "=== Collecting networking status ==="
gather "virtual networks" oc get virtualnetworks -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/virtualnetworks.txt" 2>&1
gather "virtual networks yaml" oc get virtualnetworks -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/virtualnetworks.yaml" 2>&1
gather "subnets" oc get subnets -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/subnets.txt" 2>&1
gather "subnets yaml" oc get subnets -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/subnets.yaml" 2>&1
gather "security groups" oc get securitygroups -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/securitygroups.txt" 2>&1
gather "cluster UDN" oc get clusteruserdefinednetwork -o yaml > "${ARTIFACT_DIR}/clusteruserdefinednetwork.yaml" 2>&1

echo "=== Collecting cert-manager status ==="
gather "certificates" oc get certificates -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/certificates.txt" 2>&1
gather "certificates yaml" oc get certificates -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/certificates.yaml" 2>&1
gather "routes" oc get routes -n "${NAMESPACE}" -o wide > "${ARTIFACT_DIR}/routes.txt" 2>&1
gather "keycloak routes" oc get routes -n keycloak -o wide > "${ARTIFACT_DIR}/routes-keycloak.txt" 2>&1

echo "=== Collecting node resource usage ==="
gather "node top" oc adm top node > "${ARTIFACT_DIR}/node-resources.txt" 2>&1
gather "pod top" oc adm top pod -n "${NAMESPACE}" --sort-by=memory > "${ARTIFACT_DIR}/pod-resources.txt" 2>&1
gather "nodes" oc get nodes -o wide > "${ARTIFACT_DIR}/nodes.txt" 2>&1
gather "node describe" oc describe node > "${ARTIFACT_DIR}/node-describe.txt" 2>&1

echo "=== Collecting cluster operator status ==="
gather "cluster operators" oc get co > "${ARTIFACT_DIR}/clusteroperators.txt" 2>&1
gather "CNV CSV" oc get csv -n openshift-cnv -o wide > "${ARTIFACT_DIR}/cnv/csv.txt" 2>&1

echo "=== Collecting storage diagnostics ==="
mkdir -p "${ARTIFACT_DIR}/storage"
gather "storage pods" oc get pods -n openshift-storage -o wide > "${ARTIFACT_DIR}/storage/pods.txt" 2>&1
gather "storage events" oc get events -n openshift-storage --sort-by=.lastTimestamp > "${ARTIFACT_DIR}/storage/events.txt" 2>&1
gather "LVM cluster" oc get lvmcluster -n openshift-storage -o yaml > "${ARTIFACT_DIR}/storage/lvmcluster.yaml" 2>&1
gather "LVM volume groups" oc get lvmvolumegroups -n openshift-storage -o yaml > "${ARTIFACT_DIR}/storage/lvmvolumegroups.yaml" 2>&1
gather "storage classes" oc get sc -o wide > "${ARTIFACT_DIR}/storage/storageclasses.txt" 2>&1
gather "PVs" oc get pv -o wide > "${ARTIFACT_DIR}/storage/pvs.txt" 2>&1
gather "all PVCs" oc get pvc -A -o wide > "${ARTIFACT_DIR}/storage/pvcs-all.txt" 2>&1
gather "volume attachments" oc get volumeattachments -o wide > "${ARTIFACT_DIR}/storage/volumeattachments.txt" 2>&1
for pod in $(oc get pods -n openshift-storage -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    oc logs "${pod}" -n openshift-storage > "${ARTIFACT_DIR}/storage/pod-${pod}.log" 2>&1 || true
done

echo "=== Collecting MachineConfig diagnostics ==="
mkdir -p "${ARTIFACT_DIR}/mco"
gather "MCP" oc get mcp -o wide > "${ARTIFACT_DIR}/mco/mcp.txt" 2>&1
gather "MC" oc get mc --sort-by=.metadata.creationTimestamp > "${ARTIFACT_DIR}/mco/mc.txt" 2>&1
gather "pull secret registries" bash -c "oc get secret pull-secret -n openshift-config -o jsonpath='{.data.\.dockerconfigjson}' 2>/dev/null \
    | base64 -d 2>/dev/null | jq -r '.auths | keys[]'" > "${ARTIFACT_DIR}/mco/pull-secret-registries.txt" 2>&1

echo "=== Collecting service account pull secret state ==="
gather "service accounts" oc get sa -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/serviceaccounts.yaml" 2>&1
gather "secrets types" oc get secrets -n "${NAMESPACE}" -o custom-columns='NAME:.metadata.name,TYPE:.type' > "${ARTIFACT_DIR}/secrets-types.txt" 2>&1

echo "=== Collecting AAP operator status ==="
gather "AAP status" oc get ansibleautomationplatform -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/aap-status.yaml" 2>&1
gather "automation controller" oc get automationcontroller -n "${NAMESPACE}" -o yaml > "${ARTIFACT_DIR}/automationcontroller-status.yaml" 2>&1

echo "=== Collecting AAP job stdout ==="
mkdir -p "${ARTIFACT_DIR}/aap-jobs"
AAP_ROUTE=$(oc get route osac-aap -n "${NAMESPACE}" -o jsonpath='{.spec.host}' 2>/dev/null || true)
AAP_ADMIN_PW=$(oc get secret osac-aap-controller-admin-password -n "${NAMESPACE}" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [[ -n "${AAP_ROUTE}" && -n "${AAP_ADMIN_PW}" ]]; then
    AAP_AUTH=(-sk -u "admin:${AAP_ADMIN_PW}")
    page=1
    while true; do
        page_file="${ARTIFACT_DIR}/aap-jobs/jobs-page-${page}.json"
        curl "${AAP_AUTH[@]}" \
            "https://${AAP_ROUTE}/api/controller/v2/jobs/?page=${page}&page_size=50&order_by=id" \
            > "${page_file}" 2>&1 || break
        jq -e '.results' "${page_file}" &>/dev/null || break
        for job_id in $(jq -r '.results[]?.id // empty' "${page_file}" 2>/dev/null); do
            status=$(jq -r ".results[] | select(.id == ${job_id}) | .status // \"unknown\"" "${page_file}" 2>/dev/null)
            name=$(jq -r ".results[] | select(.id == ${job_id}) | .name // \"unknown\"" "${page_file}" 2>/dev/null)
            curl "${AAP_AUTH[@]}" \
                "https://${AAP_ROUTE}/api/controller/v2/jobs/${job_id}/stdout/?format=txt" \
                > "${ARTIFACT_DIR}/aap-jobs/job-${job_id}-${status}-${name}.txt" 2>&1 || true
        done
        next=$(jq -r '.next // empty' "${page_file}" 2>/dev/null)
        [[ -z "${next}" || "${next}" == "null" ]] && break
        page=$((page + 1))
    done
    echo "  Captured stdout for $(ls "${ARTIFACT_DIR}/aap-jobs"/job-*.txt 2>/dev/null | wc -l) AAP jobs"
    curl "${AAP_AUTH[@]}" \
        "https://${AAP_ROUTE}/api/controller/v2/project_updates/?page_size=50&order_by=id" \
        > "${ARTIFACT_DIR}/aap-jobs/project-updates.json" 2>&1 || true
    for pu_id in $(jq -r '.results[]?.id // empty' "${ARTIFACT_DIR}/aap-jobs/project-updates.json" 2>/dev/null); do
        status=$(jq -r ".results[] | select(.id == ${pu_id}) | .status // \"unknown\"" \
            "${ARTIFACT_DIR}/aap-jobs/project-updates.json" 2>/dev/null)
        curl "${AAP_AUTH[@]}" \
            "https://${AAP_ROUTE}/api/controller/v2/project_updates/${pu_id}/stdout/?format=txt" \
            > "${ARTIFACT_DIR}/aap-jobs/project-update-${pu_id}-${status}.txt" 2>&1 || true
    done
    echo "  Captured $(ls "${ARTIFACT_DIR}/aap-jobs"/project-update-*.txt 2>/dev/null | wc -l) AAP project updates"
else
    echo "  AAP route or admin password not found, skipping job stdout capture"
fi

echo "=== Log collection complete ==="
