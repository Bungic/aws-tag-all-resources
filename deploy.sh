#!/usr/bin/env bash
#
# One-shot deploy script for the tagAllResources Lambda.
#
# Creates (idempotent, re-running updates):
#   - IAM execution role + inline policy for the Lambda
#   - IAM role + inline policy for EventBridge Scheduler to invoke the Lambda
#   - Lambda function (Python 3.12, 15 min timeout, 512 MB)
#   - EventBridge Scheduler schedule firing at 00:00 and 12:00 Europe/Istanbul
#
# Requires: awscli v2, zip, jq (only for nicer output).

set -euo pipefail

cd "$(dirname "$0")"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGION="${AWS_REGION:-eu-central-1}"
FUNCTION_NAME="tagAllResources"
LAMBDA_ROLE_NAME="tagAllResources-exec-role"
SCHEDULER_ROLE_NAME="tagAllResources-scheduler-role"
SCHEDULE_NAME="tagAllResources-twice-daily"
SCHEDULE_GROUP="default"

LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
SCHEDULER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SCHEDULER_ROLE_NAME}"
LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# Render schedulerTrustPolicy.json with the real account ID (confused-deputy condition).
RENDERED_SCHEDULER_TRUST="$(mktemp)"
trap 'rm -f "${RENDERED_SCHEDULER_TRUST}"' EXIT
sed "s/__ACCOUNT_ID__/${ACCOUNT_ID}/g" schedulerTrustPolicy.json > "${RENDERED_SCHEDULER_TRUST}"

echo "==> 1/5 Lambda execution role"
if aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" >/dev/null 2>&1; then
    echo "    role exists, updating trust policy"
    aws iam update-assume-role-policy \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --policy-document file://trustPolicy.json
else
    aws iam create-role \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --assume-role-policy-document file://trustPolicy.json \
        --description "Execution role for the tagAllResources Lambda" >/dev/null
fi
aws iam put-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-name "tagAllResources-inline" \
    --policy-document file://lambdaRole.json

echo "==> 2/5 Scheduler role"
if aws iam get-role --role-name "${SCHEDULER_ROLE_NAME}" >/dev/null 2>&1; then
    aws iam update-assume-role-policy \
        --role-name "${SCHEDULER_ROLE_NAME}" \
        --policy-document "file://${RENDERED_SCHEDULER_TRUST}"
else
    aws iam create-role \
        --role-name "${SCHEDULER_ROLE_NAME}" \
        --assume-role-policy-document "file://${RENDERED_SCHEDULER_TRUST}" \
        --description "Allows EventBridge Scheduler to invoke tagAllResources" >/dev/null
fi
aws iam put-role-policy \
    --role-name "${SCHEDULER_ROLE_NAME}" \
    --policy-name "invoke-tagAllResources" \
    --policy-document file://schedulerInvokePolicy.json

echo "==> 3/5 Packaging Lambda"
rm -f tagAllResources.zip
zip -q tagAllResources.zip tagAllResources.py

echo "==> 4/5 Lambda function"
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo "    function exists, updating code + config"
    aws lambda update-function-code \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}" \
        --zip-file fileb://tagAllResources.zip >/dev/null
    aws lambda wait function-updated \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}"
    aws lambda update-function-configuration \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}" \
        --timeout 900 \
        --memory-size 512 \
        --environment "Variables={DRY_RUN=false}" >/dev/null
else
    # Newly-created roles can take a few seconds to be assumable by Lambda.
    echo "    waiting briefly for IAM role to propagate..."
    sleep 10
    aws lambda create-function \
        --function-name "${FUNCTION_NAME}" \
        --region "${REGION}" \
        --runtime python3.12 \
        --role "${LAMBDA_ROLE_ARN}" \
        --handler tagAllResources.lambda_handler \
        --timeout 900 \
        --memory-size 512 \
        --environment "Variables={DRY_RUN=false}" \
        --zip-file fileb://tagAllResources.zip >/dev/null
fi

echo "==> 5/5 EventBridge Scheduler schedule (00:00 + 12:00 Europe/Istanbul)"
SCHEDULE_TARGET=$(cat <<EOF
{
    "Arn": "${LAMBDA_ARN}",
    "RoleArn": "${SCHEDULER_ROLE_ARN}",
    "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2}
}
EOF
)

if aws scheduler get-schedule \
        --name "${SCHEDULE_NAME}" \
        --group-name "${SCHEDULE_GROUP}" \
        --region "${REGION}" >/dev/null 2>&1; then
    aws scheduler update-schedule \
        --name "${SCHEDULE_NAME}" \
        --group-name "${SCHEDULE_GROUP}" \
        --region "${REGION}" \
        --schedule-expression "cron(0 0,12 * * ? *)" \
        --schedule-expression-timezone "Europe/Istanbul" \
        --flexible-time-window "Mode=OFF" \
        --state ENABLED \
        --target "${SCHEDULE_TARGET}" >/dev/null
else
    aws scheduler create-schedule \
        --name "${SCHEDULE_NAME}" \
        --group-name "${SCHEDULE_GROUP}" \
        --region "${REGION}" \
        --schedule-expression "cron(0 0,12 * * ? *)" \
        --schedule-expression-timezone "Europe/Istanbul" \
        --flexible-time-window "Mode=OFF" \
        --state ENABLED \
        --target "${SCHEDULE_TARGET}" >/dev/null
fi

echo
echo "Done."
echo "  Lambda:   ${LAMBDA_ARN}"
echo "  Schedule: arn:aws:scheduler:${REGION}:${ACCOUNT_ID}:schedule/${SCHEDULE_GROUP}/${SCHEDULE_NAME}"
echo
echo "Manual smoke test:"
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --region ${REGION} \\"
echo "      --cli-binary-format raw-in-base64-out --payload '{}' /tmp/out.json && cat /tmp/out.json"
