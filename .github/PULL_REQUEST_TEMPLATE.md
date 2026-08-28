## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The reasoning, or the problem it solves. If it changes a trading gate or a
     threshold, include the measurement that justifies it — path-aware
     (analysis/triple_barrier.py), not fixed-horizon forward returns. Those two
     methods have given opposite answers on this codebase. -->

## Checklist

- [ ] `pytest tests/ -q` passes
- [ ] New behaviour has a test that **fails when the behaviour is removed**
      (see CONTRIBUTING.md on mutation testing)
- [ ] Comments explain the *why* behind anything non-obvious
- [ ] Documentation updated if behaviour or configuration changed
- [ ] `CHANGELOG.md` updated for anything user-visible
- [ ] No credentials, hostnames, IP addresses or personal data added
- [ ] New source files carry the Apache licence header
- [ ] CLA signed (automatic prompt on your first PR)

## Risk

<!-- What could this break? If it touches order placement, risk gates or exit
     logic, say so explicitly — those paths can lose money. -->
