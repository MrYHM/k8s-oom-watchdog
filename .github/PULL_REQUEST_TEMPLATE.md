## What this changes

<!-- The behaviour change, not the diff. If it fixes an issue, link it. -->

## Why

<!-- What cluster behaviour or failure made this necessary. -->

## Checklist

- [ ] `python3 test_watchdog.py` passes
- [ ] A bug fix comes with a test that fails before the change and passes after
- [ ] No new runtime dependency (see [CONTRIBUTING.md](../CONTRIBUTING.md))
- [ ] New failure paths surface as a metric + K8s Event + log line, never a silent skip
- [ ] Docs updated if behaviour changed — including [docs/design.md](../docs/design.md) and its Chinese counterpart if the mechanism changed
- [ ] Tried on a real cluster (>= 1.33), or explain why not
