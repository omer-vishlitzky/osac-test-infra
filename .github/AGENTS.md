# .github/ Agent Context

## E2E execution

- Full-install callers run a cheap readiness job on non-draft PRs; expensive E2E
  starts only after an organization member invokes `/e2e-ready`.
- The unlock handler dispatches a trusted base-branch workflow with the current
  GitHub synthetic PR merge ref as source/test inputs; it never reruns an older
  PR workflow run.
- Pull-request target workflows use trusted base-branch YAML before starting
  self-hosted jobs.
- The same workflows run again on `merge_group`, using GitHub's fresh temporary merge-queue ref against current `main`.
- Draft PRs skip E2E. `e2e-ready` is removed on new commits.
