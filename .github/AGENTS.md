# .github/ Agent Context

## E2E execution

- Full-install callers run E2E for non-draft PRs on `opened`, `ready_for_review`, `synchronize`, and `reopened`.
- Pull-request runs use GitHub's synthetic PR merge ref and do not modify the contributor's branch.
- The same workflows run again on `merge_group`, using GitHub's fresh temporary merge-queue ref against current `main`.
- Draft PRs skip E2E. `/ok-to-test` remains the fork secret authorization command.
