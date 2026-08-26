# .github/ Agent Context

## E2E readiness gate (OSAC-3370)

- Gate checks label presence (`lgtm` or `e2e-ready`) or CodeRabbit `APPROVED` on HEAD
- `lgtm` staleness handled by Prow (removes on push); `e2e-ready` staleness handled by cleanup workflow
- `/e2e-ready` applies the `e2e-ready` label (quiet override)
- `.github/actions/check-e2e-readiness/` — Gate: `lgtm` present, `e2e-ready` present, or `coderabbitai[bot]` APPROVED on head (blocked while a human still has `CHANGES_REQUESTED`). Human APPROVED does not unlock
- `.github/workflows/e2e-ready-label-cleanup.yml` — Removes `e2e-ready` on new pushes

## Testing

Run readiness gate unit tests:

```bash
bash .github/actions/check-e2e-readiness/check-e2e-readiness-test.sh
```
