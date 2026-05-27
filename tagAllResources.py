"""
tagAllResources Lambda
----------------------
Applies a single tag (TAG_KEY=TAG_VALUE below) to every taggable resource in
every enabled region of the account.

- Iterates enabled regions in parallel (ThreadPoolExecutor).
- For each region, uses Resource Groups Tagging API (`get_resources` +
  `tag_resources`) to find any resource that does not already carry the tag and
  applies it in batches of 20 ARNs (TagResources hard limit).
- Idempotent: resources that already have the tag are skipped.
- Set DRY_RUN=true (env var) to log what would be tagged without mutating.
"""

import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Edit these two before deploying.
TAG_KEY = "REPLACE_WITH_YOUR_TAG_KEY"
TAG_VALUE = "REPLACE_WITH_YOUR_TAG_VALUE"

if TAG_KEY.startswith("REPLACE_WITH_") or TAG_VALUE.startswith("REPLACE_WITH_"):
    sys.exit("TAG_KEY and TAG_VALUE in tagAllResources.py must be set before deploying.")

TAGS = {TAG_KEY: TAG_VALUE}

# Ephemeral / out-of-scope resource types we never want to tag.
# Matched against the "<service>:<resource-type>" classification of each ARN.
EXCLUDED_RESOURCE_TYPES = frozenset(
    {
        "eks:pod",                  # k8s pods, ephemeral and replaced on every restart
        "ssm:session",              # session-manager session records, expire automatically
        "logs:log-group",           # explicitly excluded by account owner
        "s3:object",                # only buckets are tagged, not individual objects
        "ec2:network-interface",    # ENIs for pods / NAT / load balancers — ephemeral;
                                    # the parent resource (instance / ALB / NAT) is tagged instead
        # Resources returned by GetResources but NOT supported by TagResources
        # (calling tag_resources on them returns InvalidParameterException and
        # poisons the whole batch — excluding them up-front avoids the fallout).
        "payments:payment-instrument",
        "chatbot:chat-configuration",
        "cloudformation:stack",     # tagging requires `cloudformation:UpdateStack`
                                    # which is destructive — out of scope for this Lambda
                                    # (tag stacks at creation via CDK/Terraform instead)
    }
)

# Error codes that indicate "nothing actionable happened" — either a race
# where the resource disappeared, or a resource AWS simply doesn't permit us
# to tag. Counted as `transient` rather than `failed` in the summary.
TRANSIENT_ERROR_CODES = frozenset(
    {
        # Race: resource vanished between GetResources and TagResources
        "InvalidInstanceID.NotFound",
        "InvalidNetworkInterfaceID.NotFound",
        "InvalidVolumeID.NotFound",
        "InvalidSnapshot.NotFound",
        "ResourceNotFoundException",
        "NoSuchEntity",
        # Permanent: AWS-managed resource (e.g. AutoScalingManagedRule) that
        # the service refuses to tag on any request.
        "ManagedRuleException",
    }
)

# Inter-batch sleep to avoid hammering throttled APIs (especially
# elasticloadbalancing:AddTags on accounts with many listener-rules).
INTER_BATCH_SLEEP = 0.4

# Additional passes for ARNs that came back with `Throttling` in the
# FailedResourcesMap. Each pass waits longer before retrying.
THROTTLE_RETRY_SLEEPS = (3.0, 7.0, 15.0, 30.0, 60.0)

# TagResources hard limit: 20 ARNs per call.
BATCH_SIZE = 20

# Up to 8 regions tagged in parallel. Each worker is I/O bound on AWS APIs.
MAX_REGION_WORKERS = 8

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Standard botocore retry config — Tagging API is throttled aggressively.
# High max_attempts matters for ELB listener-rule tagging: ElasticLoadBalancing
# has a low tag rate limit and batches can get throttled repeatedly.
BOTO_CONFIG = Config(retries={"max_attempts": 20, "mode": "adaptive"})

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def list_enabled_regions() -> list[str]:
    """Return regions that are opted in (or always-enabled) for this account."""
    ec2 = boto3.client("ec2", region_name="eu-west-1", config=BOTO_CONFIG)
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return sorted(r["RegionName"] for r in resp["Regions"])


def classify_arn(arn: str) -> str:
    """Return a "<service>:<resource-type>" key for an ARN, matching inspect output."""
    parts = arn.split(":", 5)
    service = parts[2] if len(parts) > 2 else "?"
    rest = parts[5] if len(parts) > 5 else ""
    if service == "s3":
        # arn:aws:s3:::bucket  vs  arn:aws:s3:::bucket/key
        return "s3:object" if "/" in rest else "s3:bucket"
    if "/" in rest:
        rtype = rest.split("/", 1)[0]
    elif ":" in rest:
        rtype = rest.split(":", 1)[0]
    else:
        rtype = rest
    return f"{service}:{rtype}"


def find_untagged_global_arns() -> list[str]:
    """Enumerate global AWS services that `GetResources` doesn't reliably
    return when their resources carry no tags. Called from us-east-1 only.

    Covers:
      - CloudFront distributions
      - Route 53 hosted zones
      - Route 53 health checks
    """
    untagged: list[str] = []

    cf = boto3.client("cloudfront", config=BOTO_CONFIG)
    try:
        for page in cf.get_paginator("list_distributions").paginate():
            for dist in (page.get("DistributionList") or {}).get("Items") or []:
                arn = dist["ARN"]
                tags_resp = cf.list_tags_for_resource(Resource=arn)
                existing = {
                    t["Key"]: t["Value"]
                    for t in tags_resp.get("Tags", {}).get("Items", [])
                }
                if existing.get(TAG_KEY) != TAG_VALUE:
                    untagged.append(arn)
    except ClientError as exc:
        logger.warning("cloudfront enum skipped: %s", exc.response["Error"]["Code"])

    r53 = boto3.client("route53", config=BOTO_CONFIG)
    try:
        for page in r53.get_paginator("list_hosted_zones").paginate():
            for zone in page.get("HostedZones", []):
                zone_id = zone["Id"].rsplit("/", 1)[-1]
                tags_resp = r53.list_tags_for_resource(
                    ResourceType="hostedzone", ResourceId=zone_id
                )
                existing = {
                    t["Key"]: t["Value"]
                    for t in tags_resp["ResourceTagSet"].get("Tags", [])
                }
                if existing.get(TAG_KEY) != TAG_VALUE:
                    untagged.append(f"arn:aws:route53:::hostedzone/{zone_id}")
    except ClientError as exc:
        logger.warning("route53 zones enum skipped: %s", exc.response["Error"]["Code"])

    try:
        for page in r53.get_paginator("list_health_checks").paginate():
            for hc in page.get("HealthChecks", []):
                hc_id = hc["Id"]
                tags_resp = r53.list_tags_for_resource(
                    ResourceType="healthcheck", ResourceId=hc_id
                )
                existing = {
                    t["Key"]: t["Value"]
                    for t in tags_resp["ResourceTagSet"].get("Tags", [])
                }
                if existing.get(TAG_KEY) != TAG_VALUE:
                    untagged.append(f"arn:aws:route53:::healthcheck/{hc_id}")
    except ClientError as exc:
        logger.warning("route53 health checks enum skipped: %s", exc.response["Error"]["Code"])

    # Global Accelerator is a global service but its control-plane endpoint
    # only lives in us-west-2 — the client region is always us-west-2, even
    # when called from a Lambda running in eu-west-1.
    ga = boto3.client("globalaccelerator", region_name="us-west-2", config=BOTO_CONFIG)
    for method, key in [
        ("list_accelerators", "Accelerators"),
        ("list_custom_routing_accelerators", "Accelerators"),
    ]:
        try:
            paginator = ga.get_paginator(method)
            for page in paginator.paginate():
                for item in page.get(key, []):
                    arn = item["AcceleratorArn"]
                    tags_resp = ga.list_tags_for_resource(ResourceArn=arn)
                    existing = {t["Key"]: t["Value"] for t in tags_resp.get("Tags", [])}
                    if existing.get(TAG_KEY) != TAG_VALUE:
                        untagged.append(arn)
        except ClientError as exc:
            logger.warning(
                "global accelerator %s skipped: %s", method, exc.response["Error"]["Code"]
            )

    return untagged


def find_untagged_wafv2_arns(region: str, scope: str) -> list[str]:
    """Enumerate WAFv2 resources via the native API.

    WAFv2 is supported by the Resource Groups Tagging API, but `GetResources`
    only returns WAFv2 objects that already carry at least one tag — untagged
    WebACLs / IPSets / RuleGroups / RegexPatternSets are invisible there, so we
    list them directly and filter by their per-resource tags.

    `scope` must be "REGIONAL" (any region) or "CLOUDFRONT" (only in us-east-1).
    """
    waf = boto3.client("wafv2", region_name=region, config=BOTO_CONFIG)
    candidates: list[str] = []

    for method, key in [
        ("list_web_acls", "WebACLs"),
        ("list_ip_sets", "IPSets"),
        ("list_rule_groups", "RuleGroups"),
        ("list_regex_pattern_sets", "RegexPatternSets"),
    ]:
        next_marker = None
        while True:
            kwargs = {"Scope": scope, "Limit": 100}
            if next_marker:
                kwargs["NextMarker"] = next_marker
            resp = getattr(waf, method)(**kwargs)
            candidates.extend(item["ARN"] for item in resp.get(key, []))
            next_marker = resp.get("NextMarker")
            if not next_marker:
                break

    untagged: list[str] = []
    for arn in candidates:
        tags_resp = waf.list_tags_for_resource(ResourceARN=arn)
        tags = {t["Key"]: t["Value"] for t in tags_resp["TagInfoForResource"].get("TagList", [])}
        if tags.get(TAG_KEY) != TAG_VALUE:
            untagged.append(arn)
    return untagged


def find_untagged_arns(region: str) -> tuple[list[str], Counter]:
    """Page through every resource in `region`; return (arns_to_tag, skipped_counts)."""
    client = boto3.client("resourcegroupstaggingapi", region_name=region, config=BOTO_CONFIG)
    paginator = client.get_paginator("get_resources")
    untagged: list[str] = []
    skipped: Counter = Counter()

    for page in paginator.paginate(ResourcesPerPage=100):
        for resource in page.get("ResourceTagMappingList", []):
            tags = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
            if tags.get(TAG_KEY) == TAG_VALUE:
                continue
            arn = resource["ResourceARN"]
            rtype = classify_arn(arn)
            if rtype in EXCLUDED_RESOURCE_TYPES:
                skipped[rtype] += 1
                continue
            untagged.append(arn)

    return untagged, skipped


def _tag_single(client, arn: str) -> tuple[bool, str | None]:
    """Tag one ARN. Returns (success, error_code_if_failed)."""
    try:
        resp = client.tag_resources(ResourceARNList=[arn], Tags=TAGS)
    except ClientError as exc:
        return False, exc.response["Error"]["Code"]
    failed_map = resp.get("FailedResourcesMap") or {}
    if arn in failed_map:
        return False, failed_map[arn].get("ErrorCode", "unknown")
    return True, None


def tag_arns_in_batches(
    region: str, arns: list[str]
) -> tuple[int, dict[str, str], dict[str, str]]:
    """Apply the MAP tag in batches of 20.
    Returns (success_count, real_failures, transient_failures).
    If a batch raises InvalidParameterException (one bad ARN poisoning 19
    good ones), we retry each ARN individually to isolate the offender."""
    if not arns:
        return 0, {}, {}

    if DRY_RUN:
        logger.info("[DRY_RUN][%s] would tag %d resources", region, len(arns))
        return len(arns), {}, {}

    client = boto3.client("resourcegroupstaggingapi", region_name=region, config=BOTO_CONFIG)
    success = 0
    failures: dict[str, str] = {}
    transient: dict[str, str] = {}
    throttled: list[str] = []  # ARNs that came back with Throttling — retry later

    def _record_failure(arn: str, code: str) -> None:
        (transient if code in TRANSIENT_ERROR_CODES else failures)[arn] = code

    for i in range(0, len(arns), BATCH_SIZE):
        batch = arns[i : i + BATCH_SIZE]
        try:
            resp = client.tag_resources(ResourceARNList=batch, Tags=TAGS)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "InvalidParameterException" and len(batch) > 1:
                # One bad ARN poisoned the batch — retry each individually.
                for arn in batch:
                    ok, err = _tag_single(client, arn)
                    if ok:
                        success += 1
                    elif err == "Throttling":
                        throttled.append(arn)
                    else:
                        _record_failure(arn, err or "unknown")
            else:
                for arn in batch:
                    _record_failure(arn, f"batch_error:{code}")
            time.sleep(INTER_BATCH_SLEEP)
            continue

        failed_map = resp.get("FailedResourcesMap") or {}
        for arn, info in failed_map.items():
            code = info.get("ErrorCode", "unknown")
            if code == "Throttling":
                throttled.append(arn)
            else:
                _record_failure(arn, code)
        success += len(batch) - len(failed_map)
        time.sleep(INTER_BATCH_SLEEP)

    # Throttle retry loop: for ARNs the underlying service (usually
    # elasticloadbalancing:AddTags) rate-limited. We back off, retry each
    # individually to avoid batch cross-contamination, and sleep between calls.
    for attempt, backoff in enumerate(THROTTLE_RETRY_SLEEPS, start=1):
        if not throttled:
            break
        logger.info(
            "[%s] throttle retry %d/%d: %d ARNs after %.0fs sleep",
            region, attempt, len(THROTTLE_RETRY_SLEEPS), len(throttled), backoff,
        )
        time.sleep(backoff)
        next_round: list[str] = []
        for arn in throttled:
            ok, err = _tag_single(client, arn)
            if ok:
                success += 1
            elif err == "Throttling":
                next_round.append(arn)
            else:
                _record_failure(arn, err or "unknown")
            time.sleep(0.2)
        throttled = next_round

    # Anything still throttled after all passes — give up and record.
    for arn in throttled:
        _record_failure(arn, "Throttling")

    return success, failures, transient


def tag_global_accelerator_arns(arns: list[str]) -> tuple[int, dict[str, str]]:
    """Global Accelerator is returned by our global-resource enumeration but
    is NOT supported by resourcegroupstaggingapi:TagResources (the API returns
    InvalidParameterException). Tag them natively via the GA client instead."""
    if not arns:
        return 0, {}
    if DRY_RUN:
        logger.info("[DRY_RUN][globalaccelerator] would tag %d accelerators", len(arns))
        return len(arns), {}

    ga = boto3.client("globalaccelerator", region_name="us-west-2", config=BOTO_CONFIG)
    success = 0
    failures: dict[str, str] = {}
    for arn in arns:
        try:
            ga.tag_resource(ResourceArn=arn, Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])
            success += 1
        except ClientError as exc:
            failures[arn] = exc.response["Error"]["Code"]
    return success, failures


def process_region(region: str) -> dict:
    """Find + tag for one region. Returns a result summary."""
    try:
        untagged, skipped = find_untagged_arns(region)
        # WAFv2 isn't indexed by GetResources unless already tagged — enumerate
        # natively. REGIONAL scope exists in every region; CLOUDFRONT scope is
        # only queryable from us-east-1 and covers global/CloudFront WAF.
        try:
            untagged.extend(find_untagged_wafv2_arns(region, "REGIONAL"))
            if region == "us-east-1":
                untagged.extend(find_untagged_wafv2_arns(region, "CLOUDFRONT"))
                # Global services (CloudFront, Route53) are accessed via us-east-1.
                untagged.extend(find_untagged_global_arns())
        except ClientError as exc:
            logger.warning("[%s] wafv2 enumeration skipped: %s", region, exc.response["Error"]["Code"])

        # Global Accelerator ARNs must be tagged via the native GA client;
        # the Resource Groups Tagging API rejects them with InvalidParameterException.
        ga_arns = [a for a in untagged if ":globalaccelerator::" in a]
        other_arns = [a for a in untagged if ":globalaccelerator::" not in a]

        success, failures, transient = tag_arns_in_batches(region, other_arns)
        if ga_arns:
            ga_success, ga_failures = tag_global_accelerator_arns(ga_arns)
            success += ga_success
            failures.update(ga_failures)
        result = {
            "region": region,
            "scanned_untagged": len(untagged),
            "tagged": success,
            "failed": len(failures),
            "transient": len(transient),
            "skipped": dict(skipped),
        }
        if failures:
            sample = dict(list(failures.items())[:10])
            logger.warning("[%s] %d real failures (sample): %s", region, len(failures), sample)
        if transient:
            logger.info("[%s] %d transient (race) skips: %s", region, len(transient),
                        dict(list(transient.items())[:5]))
        logger.info("[%s] done: %s", region, result)
        return result
    except ClientError as exc:
        # Region might not have the API enabled, or credentials lack access.
        code = exc.response["Error"]["Code"]
        logger.error("[%s] aborted: %s", region, code)
        return {"region": region, "error": code}


def lambda_handler(event, context):
    regions = list_enabled_regions()
    logger.info("Scanning %d regions (dry_run=%s): %s", len(regions), DRY_RUN, regions)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_REGION_WORKERS) as pool:
        futures = {pool.submit(process_region, r): r for r in regions}
        for fut in as_completed(futures):
            results.append(fut.result())

    summary = {
        "dry_run": DRY_RUN,
        "regions": len(regions),
        "total_tagged": sum(r.get("tagged", 0) for r in results),
        "total_failed": sum(r.get("failed", 0) for r in results),
        "total_transient": sum(r.get("transient", 0) for r in results),
        "per_region": sorted(results, key=lambda x: x["region"]),
    }
    logger.info(
        "SUMMARY: tagged=%d failed=%d transient=%d",
        summary["total_tagged"], summary["total_failed"], summary["total_transient"],
    )
    return summary


if __name__ == "__main__":
    # Local smoke test: requires AWS creds in environment.
    import json

    print(json.dumps(lambda_handler({}, None), indent=2, default=str))
