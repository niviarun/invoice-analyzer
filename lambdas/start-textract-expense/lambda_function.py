import os
import boto3

# Set region to match your bucket location
textract = boto3.client("textract", region_name=os.environ.get("AWS_REGION", "us-west-1"))

RAW_BUCKET = os.environ.get("RAW_BUCKET", "your-invoice-bucket")


def lambda_handler(event, context):
    action = event.get("action")

    if action == "START":
        s3_key = event.get("s3_key")
        s3_bucket = event.get("bucket", RAW_BUCKET)

        # Sanitize the key just in case
        s3_key = s3_key.strip("/")

        try:
            resp = textract.start_expense_analysis(
                DocumentLocation={
                    "S3Object": {
                        "Bucket": s3_bucket,
                        "Name": s3_key,
                    }
                }
            )
            return {
                "JobId": resp["JobId"],
                "Status": "IN_PROGRESS",
                "bucket": s3_bucket,
                "s3_key": s3_key,
            }
        except Exception as e:
            print(f"Textract Error: {str(e)}")
            raise e

    if action == "CHECK":
        job_id = event.get("JobId")
        resp = textract.get_expense_analysis(JobId=job_id)
        status = resp.get("JobStatus", "UNKNOWN").upper()
        return {"JobId": job_id, "Status": resp.get("JobStatus", "UNKNOWN")}

    raise ValueError(f"Unknown action: {action}")
