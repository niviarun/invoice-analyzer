import json
import os
import boto3
import gspread
from datetime import datetime

s3 = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID")
SECRET_NAME = os.environ.get("SECRET_NAME", "your-secret-name")


def get_google_creds():
    secret_value = secrets_client.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(secret_value["SecretString"])


def lambda_handler(event, context):
    try:
        # 1. Look at the 'result' object to find where the JSON is stored
        result_info = event.get("result", {})
        output_info = result_info.get("output", {})

        res_bucket = output_info.get("bucket")
        res_key = output_info.get("json_key")

        if not res_key:
            raise ValueError(
                "Could not find the JSON output key in the event result."
            )

        # 2. Download the actual extracted invoice data from S3
        print(f"Downloading results from: s3://{res_bucket}/{res_key}")
        file_obj = s3.get_object(Bucket=res_bucket, Key=res_key)
        full_payload = json.loads(file_obj["Body"].read().decode("utf-8"))

        # 3. Drill down into the payload (matching the process-textract structure)
        invoice_data = full_payload.get("data", {})
        summary = invoice_data.get("summary", {})
        vendor = invoice_data.get("vendor", {})

        # 4. Authenticate with Google
        creds = get_google_creds()
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.get_worksheet(0)

        # 5. Prepare the row
        now = datetime.utcnow()
        year = now.strftime("%Y")
        month = now.strftime("%m")
        day = now.strftime("%d")

        # Invoice details
        vendor_name = vendor.get("name", {}).get("value", "N/A")
        invoice_id = summary.get("invoice_id", {}).get("value", "N/A")
        total_val = summary.get("total", {}).get("value", "0.00")
        po_num = summary.get("po_number", {}).get("value", "N/A")
        tax = summary.get("tax_data", {}).get("value", "0.00")
        freight = summary.get("freight_amount", {}).get("value", "0.00")
        item_count = invoice_data.get("line_item_count", 0)

        # SAP logic
        match_cat = event.get("match_results", {}).get("category", "PENDING")

        # S3 Link
        s3_link = f"https://s3.console.aws.amazon.com/s3/object/{res_bucket}?prefix={res_key}"

        # Constructing the row
        row = [
            year,
            month,
            day,
            vendor_name,
            invoice_id,
            total_val,
            tax,
            freight,
            item_count,
            po_num,
            match_cat,
            s3_link,
        ]
        worksheet.append_row(row, value_input_option="USER_ENTERED")

        return {"status": "success", "invoice_id": invoice_id}

    except Exception as e:
        print(f"Logger Error: {str(e)}")
        raise e
