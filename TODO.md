# TODO

| Item | Why | Notes |
| --- | --- | --- |
| Response models for the endpoints `crimson-client` parses (`/seasons`, `/info`, `/trending`, `/search/*`, the overviews) | A renamed key fails only as a blank page in production | A `response_model` filters undeclared fields, so derive each from the observed payload and diff before and after, one endpoint at a time. Then generate client types from `openapi.json`. |
| Shared rate-limit storage | Limits are per replica, so `30/minute` on `/watch` is really that times the replica count | Point `RATE_LIMIT_STORAGE_URI` at Redis. The login wall's session cache is per replica too, which is fine (60s TTL). |
| Run the CI gate on merge requests | It runs on `main` and tags only, so a broken branch is found after merge | `.gitlab-ci.yml`, `gate` job rules |
