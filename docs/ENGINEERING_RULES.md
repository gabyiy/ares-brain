ARES Engineering Rules

These rules apply to all future ARES development work.

1. Never skip failing tests.
2. Never mark tests as xfail or skip unless Gabi explicitly approves it.
3. Never remove assertions to make tests pass.
4. Never hide errors with broad try/except blocks.
5. Fix the root cause, not the symptom.
6. Every change must keep the full verification suite passing:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```

7. Update `README.md` and `docs/SESSION_HANDOFF.md` after every meaningful change.
8. Commit logical changes separately.
9. Push only after all checks pass.
10. If a test fails, explain:
    - Root cause
    - Fix
    - Risk
    - Full verification result

No roadmap work, new features, or voice work should begin until these rules are satisfied for the current change.
