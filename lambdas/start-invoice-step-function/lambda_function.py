import json
import os
import boto3
import urllib.parse

sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def lambda_handler(event, context):
    started = 0
    skipped = 0

    for record in event.get("Records", []):
        # Safety check: ensure this is an S3 event
        if record.get("eventSource") != "aws:s3":
            continue

        bucket = record["s3"]["bucket"]["name"]
        # Decode the filename (handles spaces/special characters)
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        # Only start the Step Function if the file is in 'raw/'
        # SES emails in 'emails/' will be skipped by this script.
        if not key.startswith("raw/"):
            print(f"Skipping non-raw file: {key}")
            skipped += 1
            continue

        execution_input = {
            "s3_key": key,       # The path to the PDF
            "bucket": bucket,    # The bucket name
        }

        sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            input=json.dumps(execution_input),
        )
        print(f"Started Step Function for: {key}")
        started += 1

    return {"ok": True, "started": started, "skipped": skipped}
