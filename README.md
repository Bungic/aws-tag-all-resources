# tagAllResources

A Lambda that walks every enabled region in your AWS account and applies one tag to every taggable resource it finds. It is idempotent, so you can run it as often as you want without re-tagging anything.

Why this exists: AWS gives you several tagging tools, but none of them sweeps the whole account in one pass. The Resource Groups Tagging API gets close, but it does not return resources that have zero tags, and a handful of services (WAFv2, CloudFront, Route 53, Global Accelerator) need their own client calls. This Lambda glues all of that together.

## What it does

1. Lists every region opted in for the account.
2. For each region, up to 8 in parallel, calls `resourcegroupstaggingapi:GetResources`, finds resources missing your tag, and applies it with `tag_resources` in batches of 20 (AWS hard limit).
3. Tags WAFv2, CloudFront, Route 53 zones and health checks, and Global Accelerator directly. The Tagging API does not return them when they have no tags.
4. Retries throttled batches with five backoff passes (3 to 60 seconds). ELB `AddTags` is usually the throttle source.
5. Returns a per-region summary: scanned, tagged, failed, transient.

## What you need to set

Open `tagAllResources.py` and edit two lines near the top:

```python
TAG_KEY = "REPLACE_WITH_YOUR_TAG_KEY"
TAG_VALUE = "REPLACE_WITH_YOUR_TAG_VALUE"
```

The Lambda refuses to start if either still contains `REPLACE_WITH_`, so you cannot accidentally tag your whole account with placeholder strings.

Do the same edit in `inspectResources.py` if you want to run the local inspection helper.

Optional environment variable on the Lambda:

| Variable | Default | What it does |
|---|---|---|
| `DRY_RUN` | `false` | When `true`, logs what would be tagged but does not call `TagResources` |

## Deploy

```bash
./deploy.sh
```

The script reads your AWS account ID from `aws sts get-caller-identity`. Override the region with `AWS_REGION=eu-west-1 ./deploy.sh` if you want something other than the default `eu-central-1`.

It is idempotent: re-running updates the Lambda code, IAM policies, and the EventBridge Scheduler in place. Default schedule fires twice a day, at 00:00 and 12:00 Europe/Istanbul.

Smoke test after deploy:

```bash
aws lambda invoke \
    --function-name tagAllResources \
    --region eu-central-1 \
    --cli-binary-format raw-in-base64-out \
    --payload '{}' \
    /tmp/out.json && cat /tmp/out.json
```

Response shape:

```json
{
    "dry_run": false,
    "regions": 17,
    "total_tagged": 423,
    "total_failed": 2,
    "per_region": [
        {"region": "eu-west-1", "scanned_untagged": 120, "tagged": 120, "failed": 0}
    ]
}
```

## What it does not do

- **CloudFormation stacks.** Tagging a stack needs `cloudformation:UpdateStack`, which can recreate resources. Tag stacks at creation time via CDK or Terraform instead.
- **CloudWatch log groups.** Excluded by default. Edit `EXCLUDED_RESOURCE_TYPES` in `tagAllResources.py` if you want them in.
- **S3 objects.** Only buckets are tagged. Object-level tagging is a different scale of problem.
- **IAM resources across regions.** IAM is global. The Lambda tags IAM resources once from `us-east-1`; passes from other regions are no-ops.

## Known sharp edges

- `tag_resources` has no partial-success guarantee for invalid ARNs. One malformed ARN can poison a batch of 20 with `InvalidParameterException`. The Lambda retries each ARN individually in that case to find the offender.
- ELB listener-rule tagging has a low rate limit. On accounts with hundreds of listener rules, the throttle retry loop does real work. Five backoff passes are configured (3s, 7s, 15s, 30s, 60s); raise them if you still see timeouts.
- Lambda timeout is 15 minutes. At ~30 enabled regions with tens of thousands of resources you can get close. If you do, lower `MAX_REGION_WORKERS` (less parallelism, more throttle headroom) or shard regions across multiple invocations.

## Files

| File | What it is |
|---|---|
| `tagAllResources.py` | Lambda handler |
| `inspectResources.py` | Local script: count untagged resources by service/type in one region |
| `lambdaRole.json` | Inline policy for the Lambda execution role |
| `trustPolicy.json` | Lambda service trust policy |
| `schedulerInvokePolicy.json` | Lets EventBridge Scheduler invoke this function |
| `schedulerTrustPolicy.json` | Scheduler trust policy template (`__ACCOUNT_ID__` rendered by `deploy.sh`) |
| `deploy.sh` | Creates or updates IAM roles, the Lambda, and the schedule |

## License

MIT. See [LICENSE](LICENSE).
