"""
Ad-hoc inspection: group untagged resources in a region by service + resource-type.

Run: python3 inspectResources.py [region]   (default: eu-central-1)
"""

import sys
from collections import Counter

import boto3
from botocore.config import Config

TAG_KEY = "REPLACE_WITH_YOUR_TAG_KEY"
TAG_VALUE = "REPLACE_WITH_YOUR_TAG_VALUE"

region = sys.argv[1] if len(sys.argv) > 1 else "eu-central-1"
client = boto3.client(
    "resourcegroupstaggingapi",
    region_name=region,
    config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
)

counts: Counter = Counter()
samples: dict[str, str] = {}
total = 0

for page in client.get_paginator("get_resources").paginate(ResourcesPerPage=100):
    for r in page.get("ResourceTagMappingList", []):
        tags = {t["Key"]: t["Value"] for t in r.get("Tags", [])}
        if tags.get(TAG_KEY) == TAG_VALUE:
            continue
        total += 1
        arn = r["ResourceARN"]
        # arn:aws:<service>:<region>:<acct>:<rest>
        parts = arn.split(":", 5)
        service = parts[2] if len(parts) > 2 else "?"
        rest = parts[5] if len(parts) > 5 else ""
        # resource-type is usually the first token before "/" or ":"
        if "/" in rest:
            rtype = rest.split("/", 1)[0]
        elif ":" in rest:
            rtype = rest.split(":", 1)[0]
        else:
            rtype = rest
        # S3 special case: arn:aws:s3:::bucket  or  arn:aws:s3:::bucket/key
        if service == "s3":
            rtype = "object" if "/" in rest else "bucket"
        key = f"{service}:{rtype}"
        counts[key] += 1
        samples.setdefault(key, arn)

print(f"\nRegion: {region}  |  total untagged: {total}\n")
print(f"{'count':>8}  {'type':<35}  sample ARN")
print("-" * 120)
for key, n in counts.most_common():
    print(f"{n:>8}  {key:<35}  {samples[key]}")
