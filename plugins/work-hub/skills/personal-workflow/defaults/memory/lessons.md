# Reusable lessons

## Verify source authentication before treating zero results as an empty inbox

A search API can return zero visible items while a separate identity endpoint rejects the token. Confirm authentication first; report a failed source as an error, not as no work. Evidence: the personal-workflow collector's Jira `/myself` preflight.
