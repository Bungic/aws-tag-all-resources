# Changelog

## [0.1.0] - 2026-05-20

Initial public release.

- Tags every taggable resource in every enabled region.
- Handles WAFv2, CloudFront, Route 53 zones and health checks, and Global Accelerator natively (these are invisible to the Tagging API when untagged).
- Throttle retry with five backoff passes (3, 7, 15, 30, 60 seconds).
- Dry run mode via `DRY_RUN` Lambda env var.
- Idempotent `deploy.sh` that reads account ID from STS and renders the scheduler trust policy at deploy time.
- Lambda fails fast at startup if `TAG_KEY` or `TAG_VALUE` still contains the placeholder string.
