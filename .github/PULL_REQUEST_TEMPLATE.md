## Summary

(What changes does this PR introduce? Why?)

## Changes

- (bullet list of the meaningful changes - not a git log dump)

## How I tested it

- [ ] `ruff check .` is clean
- [ ] `python3 -m pytest` passes locally
- [ ] Manually exercised the affected code path
- [ ] Added or updated tests where appropriate

## Backward compatibility

- [ ] Schema changes are additive (new columns are nullable, no DROP / RENAME)
- [ ] Wire-format changes carry a version bump and the receiver still accepts older payloads
- [ ] CLI changes do not break existing flags
- [ ] CHANGELOG.md updated under `## [Unreleased]`

## Related issues

Closes #
