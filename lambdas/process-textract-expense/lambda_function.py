import os
import json
import csv
import io
import boto3
from datetime import datetime

textract = boto3.client("textract")
s3 = boto3.client("s3")

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "your-invoice-bucket")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "processed")


def _coerce_text(x):
    if x is None:
        return ""
    return str(x).replace("\n", " ").strip()


def _missing(v):
    if v is None:
        return True
    s = str(v).strip()
    if s == "":
        return True
    return s.upper() in {"N/A", "NA", "NONE", "NULL"}


def _find_summary_field(summary_fields, field_type):
    for field in summary_fields:
        if field.get("Type", {}).get("Text") == field_type:
            vd = field.get("ValueDetection") or {}
            return {
                "value": _coerce_text(vd.get("Text") or "N/A") or "N/A",
                "confidence": float(vd.get("Confidence") or 0.0),
            }
    return {"value": "N/A", "confidence": 0.0}


def _find_grouped_field(summary_fields, group_type, field_type):
    for field in summary_fields:
        group_props = field.get("GroupProperties") or []
        for prop in group_props:
            if group_type in (prop.get("Types") or []):
                if field.get("Type", {}).get("Text") == field_type:
                    vd = field.get("ValueDetection") or {}
                    return {
                        "value": _coerce_text(vd.get("Text") or "N/A") or "N/A",
                        "confidence": float(vd.get("Confidence") or 0.0),
                    }
    return {"value": "N/A", "confidence": 0.0}


def _paginate_expense_analysis(job_id):
    expense_documents = []
    document_metadata = None
    warnings = None
    next_token = None
    last_status = None
    last_status_message = None

    while True:
        if next_token:
            page = textract.get_expense_analysis(JobId=job_id, NextToken=next_token)
        else:
            page = textract.get_expense_analysis(JobId=job_id)

        last_status = page.get("JobStatus", last_status)
        last_status_message = page.get("StatusMessage", last_status_message)
        document_metadata = page.get("DocumentMetadata") or document_metadata
        warnings = page.get("Warnings") or warnings

        expense_documents.extend(page.get("ExpenseDocuments", []) or [])
        next_token = page.get("NextToken")
        if not next_token:
            break

    return {
        "JobId": job_id,
        "JobStatus": last_status,
        "StatusMessage": last_status_message,
        "DocumentMetadata": document_metadata,
        "Warnings": warnings,
        "ExpenseDocuments": expense_documents,
    }


def _normalize(expense_documents):
    warnings = []

    all_summary_fields = []
    for doc in expense_documents:
        all_summary_fields.extend(doc.get("SummaryFields", []) or [])

    line_item_count = 0
    for doc in expense_documents:
        for group in doc.get("LineItemGroups") or []:
            line_item_count += len(group.get("LineItems") or [])

    total_freight_val = 0.0
    for field in all_summary_fields:
        f_type = field.get("Type", {}).get("Text", "")
        if f_type in ["FREIGHT", "SHIPPING", "HANDLING"]:
            val_str = field.get("ValueDetection", {}).get("Text", "0")
            try:
                clean_val = val_str.replace("$", "").replace(",", "").strip()
                total_freight_val += float(clean_val)
            except ValueError:
                continue

    freight_dict = {"value": f"{total_freight_val:.2f}", "confidence": 1.0}

    def require(field_dict, name):
        if _missing(field_dict.get("value")):
            warnings.append(f"Required field '{name}' not found in document.")
        return field_dict

    summary = {
        "invoice_id": require(
            _find_summary_field(all_summary_fields, "INVOICE_RECEIPT_ID"),
            "summary.invoice_id",
        ),
        "invoice_date": require(
            _find_summary_field(all_summary_fields, "INVOICE_RECEIPT_DATE"),
            "summary.invoice_date",
        ),
        "due_date": _find_summary_field(all_summary_fields, "DUE_DATE"),
        "total": require(
            _find_summary_field(all_summary_fields, "TOTAL"), "summary.total"
        ),
        "po_number": _find_summary_field(all_summary_fields, "PO_NUMBER"),
        "tax_data": _find_summary_field(all_summary_fields, "TAX"),
        "freight_amount": freight_dict,
    }

    vendor = {
        "name": require(
            _find_grouped_field(all_summary_fields, "VENDOR", "NAME"), "vendor.name"
        ),
        "address": _find_grouped_field(all_summary_fields, "VENDOR", "ADDRESS"),
        "tax_id": _find_summary_field(all_summary_fields, "TAX_PAYER_ID"),
    }

    receiver = {
        "name": _find_grouped_field(
            all_summary_fields, "RECEIVER_BILL_TO", "NAME"
        ),
        "billing_address": _find_grouped_field(
            all_summary_fields, "RECEIVER_BILL_TO", "ADDRESS"
        ),
        "shipping_address": _find_grouped_field(
            all_summary_fields, "RECEIVER_SHIP_TO", "ADDRESS"
        ),
    }

    line_items = []
    for doc in expense_documents:
        for group in doc.get("LineItemGroups") or []:
            for item in group.get("LineItems") or []:
                item_details = {}
                for field in item.get("LineItemExpenseFields") or []:
                    raw_type = _coerce_text(
                        field.get("Type", {}).get("Text") or ""
                    )
                    key = raw_type.lower() if raw_type else "unknown"
                    vd = field.get("ValueDetection") or {}
                    raw_value = _coerce_text(vd.get("Text") or "")
                    item_details[key] = {
                        "value": raw_value if raw_value else "N/A",
                        "confidence": float(vd.get("Confidence") or 0.0),
                    }
                if item_details:
                    line_items.append(item_details)

    normalized = {
        "summary": summary,
        "vendor": vendor,
        "receiver": receiver,
        "line_items": line_items,
        "line_item_count": line_item_count,
    }
    return normalized, warnings


def _write_json(bucket, key, obj):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _write_csv_line_items(bucket, key, normalized):
    line_items = normalized.get("line_items") or []
    keys = sorted({k for item in line_items for k in (item or {}).keys()})

    buf = io.StringIO()
    w = csv.writer(buf)

    header = []
    for k in keys:
        header.extend([f"{k}.value", f"{k}.confidence"])
    w.writerow(header)

    for item in line_items:
        row = []
        for k in keys:
            cell = item.get(k) or {}
            row.extend([cell.get("value", ""), cell.get("confidence", 0.0)])
        w.writerow(row)

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )


def lambda_handler(event, context):
    job_id = event["JobId"]

    raw = _paginate_expense_analysis(job_id)

    status = raw.get("JobStatus") or "UNKNOWN"
    if status not in ("SUCCEEDED", "PARTIAL_SUCCESS"):
        return {
            "JobId": job_id,
            "Status": status,
            "StatusMessage": raw.get("StatusMessage"),
        }

    normalized, norm_warnings = _normalize(raw.get("ExpenseDocuments") or [])

    out_base = f"{OUTPUT_PREFIX.rstrip('/')}/{job_id}"
    json_key = f"{out_base}/result.json"
    csv_key = f"{out_base}/line_items.csv"

    payload = {
        "job": {
            "job_id": job_id,
            "job_status": status,
            "processed_at": datetime.utcnow().isoformat() + "Z",
        },
        "warnings": norm_warnings,
        "document_metadata": raw.get("DocumentMetadata"),
        "data": normalized,
    }

    _write_json(OUTPUT_BUCKET, json_key, payload)
    _write_csv_line_items(OUTPUT_BUCKET, csv_key, normalized)

    return {
        "JobId": job_id,
        "Status": status,
        "output": {
            "bucket": OUTPUT_BUCKET,
            "json_key": json_key,
            "csv_key": csv_key,
        },
        "warnings_count": len(norm_warnings),
    }
