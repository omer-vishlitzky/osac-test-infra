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
Three tests still fail. They are narrow product bugs, not vcluster problems.
We list them below.

**Runs can be made in parallel.** We made four runs at the same time on one
node. We then ran test suites against many runs at once.

**The code for BMaaS and CaaS is written and pushed.** It has not run
against a live run yet. That is the next step.

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
# Make a run called myrun1. This makes the vcluster and installs OSAC in it.
bash scripts/vcluster-timing/timed-run.sh myrun1 9460 install-only

# Run the VMaaS tests against it.
bash scripts/vcluster-timing/run-suite.sh myrun1 vmaas/sanity

# When you are done, remove the run. The outer cluster stays.
bash scripts/vcluster-run-teardown.sh myrun1
```

## The three failing tests (known, small)

1. A network name test: the server should say no when you reuse a name that
   is being deleted. It does not say no in time.
2. A disk test: one way of asking for extra disks gets stuck. The machine
   never finishes.
3. A billing test: one volume bill never arrives.

## What each suite still needs

- **VMaaS**: only the three fixes above.
- **BMaaS**: the run needs fake metal machines. The script for that is
  `scripts/vcluster-run-bmhs.sh`. The tests need one setting:
  `OSAC_BMH_KUBECONFIG` must point at the outer cluster.
- **CaaS**: the run needs the DNS settings (the BIND server is ready). The
  tests need `OSAC_TARGET_KUBECONFIG` set to the outer cluster.

## What is still missing

- The BMaaS and CaaS tests have never run against a run. They will find
  problems. We will fix them as they show up.
- The CI job that runs many tests at once is not written yet. The scripts
  are the start of it.
- The nightly reset of the outer cluster is not set up. It would clean up
  old runs and keep the AAP fast.
- The vcluster API needs a route so runners do not need a port-forward.
  Today the runner starts its own port-forward, which works but is a
  workaround.

## Where the commits are

- osac PR #2566, branch `vcluster-e2e`: the OSAC fixes, the suite changes,
  the DNS driver, the CaaS profile.
- osac-test-infra PR #854, branch `vcluster-e2e`: the run scripts, the
  teardown, the timing tools.

Every commit says what problem it fixes and what it changes. Read the commit
list top to bottom to see the whole story.
