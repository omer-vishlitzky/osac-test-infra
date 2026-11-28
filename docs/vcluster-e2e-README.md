# OSAC tests on one shared cluster

## The idea

Today, every test run gets its own whole cluster. That is slow and it wastes
big machines.

The new way: one big cluster stays up all the time. Every test run gets a
small private cluster inside it. We call it a "run". Each run has its own
OSAC. The runs cannot see each other. Many runs can live on the same big
cluster at the same time.

The big cluster is called the "outer cluster". The private cluster inside it
is called a "vcluster".

## What works today

**VMaaS is green.** The full test suite ran against a run and 126 of 134
tests passed. A test user logs in, asks for a machine, and OSAC makes a real
machine on the outer cluster. Only the run that asked for it can see it.
The last few failures were test bugs or node capacity problems, not OSAC
bugs. All are fixed or skipped now.

**CaaS got far.** The first CaaS run created a real hosted cluster on the
outer cluster. The hosted cluster reached "Available". Two OSAC bugs block
the rest. We list them below.

**BMaaS got far.** The fake metal machines work. Both reached "available"
and were seen by the run. One OSAC bug blocks the tests. We list it below.

**Runs can be made in parallel.** We made many runs at the same time on one
node and ran test suites against them at once.

## The three parts

1. **The osac repo** (PR #2566). It has the fixes that make OSAC itself work
   inside a vcluster. It also has the test changes that let the test suites
   talk to the right cluster.
2. **The osac-test-infra repo** (PR #854). It has the scripts that make and
   remove runs. It also has the AAP and Keycloak setup for each run.
3. **The outer cluster.** It holds the things every run shares: the AAP, the
   user login (Keycloak), the message bus (Kafka), the databases, the vault.
   For BMaaS it also has the bare metal parts (Ironic). For CaaS it also has
   the hosted cluster parts (MCE, HyperShift, assisted service) and the DNS
   server (BIND).

## How to run one test

```
# Make a run called myrun1. The name must end in v (VMaaS), b (BMaaS) or
# c (CaaS) before the attempt digit.
bash scripts/vcluster-timing/timed-run.sh myrunv1 9460 install-only

# Run the VMaaS tests against it.
bash scripts/vcluster-timing/run-suite.sh myrunv1 vmaas/sanity

# When you are done, remove the run. The outer cluster stays.
bash scripts/vcluster-run-teardown.sh myrunv1
```

## The open OSAC bugs (found by the first CaaS and BMaaS runs)

1. **CaaS: cross-cluster ownerReferences.** The operator creates the InfraEnv
   on the outer cluster and marks it as owned by the ClusterOrder. But the
   ClusterOrder lives in the vcluster. The outer cluster does not know it, so
   its garbage collector deletes the InfraEnv every 30 seconds. No worker ever
   joins the hosted cluster because of this.
2. **CaaS: the guest ingress VIP range cannot be set.** The chart has no key
   for it, so the hosted cluster job fails at the external access step.
3. **BMaaS: tenant setup is not idempotent in the vault.** The tenant
   reconciler fails to store the break-glass credentials when the vault paths
   already exist. The error is swallowed as "vault operation failed". The
   tenant never finishes, and the tests hang at setup.

## What each suite still needs

- **VMaaS**: nothing. It is green.
- **BMaaS**: the vault bug above. Then the suite should run.
- **CaaS**: the two bugs above. Then the hosted cluster needs workers, which
  needs the InfraEnv fix.

## What is still missing

- The three OSAC bugs above.
- The CI job that runs many tests at once is not written yet. The scripts
  are the start of it.
- The nightly reset of the outer cluster is not set up. It would clean up
  old runs and keep the AAP fast.
- The vcluster API needs a route so runners do not need a port-forward.
  Today the runner starts its own port-forward, which works but is a
  workaround.
- The BMF operator image and the vcluster compat objects (the service CA
  ConfigMap, the unprefixed credentials Secret) must be part of the install,
  not later fixes.

## Where the commits are

- osac PR #2566, branch `vcluster-e2e`: the OSAC fixes, the suite changes,
  the DNS driver, the CaaS profile.
- osac-test-infra PR #854, branch `vcluster-e2e`: the run scripts, the
  teardown, the timing tools.

Every commit says what problem it fixes and what it changes. Read the commit
list top to bottom to see the whole story.
