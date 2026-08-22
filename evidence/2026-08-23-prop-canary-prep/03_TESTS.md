# Tests

Command:

`python -m unittest tests_prop_canary.py tests_notifications.py tests_phase55.py tests_phase55b.py`

Result: **126 OK** in ~1.8s.

`tests_prop_canary.py` covers:

1. default DISARMED / PROP_LOCKED
2. restart resets DISARMED
3. missing canary flag blocks
4. `PROP_EXECUTION=false` does not block the narrow canary; `true` on the canary context is rejected
5. wrong account blocked
6. missing account identity blocked
7. second FundedNext account blocked
8. Sim101 blocked from canary (and inverse: Sim101 ATI cannot target FN)
9. wrong instrument blocked
10. NQ execution blocked
11. qty 0 blocked
12. qty 2 (FAST) blocked
13. qty 1 allowed structurally
14. stale account state blocked
15. unknown MLL blocked
16. open FundedNext position blocked
17. working order blocked
18. unsafe recon (including ambiguous Sim101 `FLAT_SAFE`) blocked
19. stale market blocked
20. warmup blocked
21. shadow blocked
22. historical blocked
23. replay blocked
24. non-phase54_live blocked
25. pre-arm signal blocked
26. post-arm genuine phase54_live reaches dry-run boundary
27. second signal blocked after one-shot latch
28. rejection disarms
29. execution exception disarms
30. disconnect disarms
31. stale market while armed disarms
32. stop-rejection → CRITICAL
33. Telegram failure cannot alter execution state
34. no automatic transition to general prop execution

Plus: AUTO/wildcard fail-closed, login/id mismatch, emergency flatten targets FN not Sim101, closed-market dry-run fails closed, frozen hash unchanged.
