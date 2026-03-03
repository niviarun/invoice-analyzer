# Invoice Analyzer

An automated invoice processing pipeline built on AWS. When a PDF invoice is uploaded to an S3 bucket, the system automatically extracts structured data using Amazon Textract, normalizes the results, and logs them to a Google Sheet — all orchestrated by an AWS Step Function.

---

## Architecture Overview

```
S3 Upload (raw/)
    │
    ▼
┌──────────────────────────────┐
│  Lambda: S3 Trigger          │  ← Detects new files in raw/, starts the pipeline
│  (start-invoice-step-func)   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Step Function:              │
│  InvoiceAnalyzer             │
│                              │
│  1. Start Textract Job  ─────┼──► Lambda: start-textract-expense
│  2. Poll for Completion      │       (START + CHECK actions)
│  3. Extract & Normalize  ────┼──► Lambda: process-textract-expense
│  4. SAP Match (mock)         │
│  5. Log to Google Sheets ────┼──► Lambda: invoice-logger-sheets
└──────────────────────────────┘
               │
               ▼
        Google Sheets
   (invoice tracking log)
```

---

## Repository Structure

```
invoice-analyzer/
├── README.md
├── .env.example                          # Template for environment variables
├── .gitignore
│
├── state-machine/
│   └── invoice-analyzer.asl.json         # Step Function definition (ASL)
│
├── lambdas/
│   ├── start-textract-expense/
│   │   └── lambda_function.py            # Starts & polls Textract expense jobs
│   │
│   ├── process-textract-expense/
│   │   └── lambda_function.py            # Normalizes Textract output → JSON + CSV
│   │
│   ├── invoice-logger-sheets/
│   │   ├── lambda_function.py            # Logs invoice data to Google Sheets
│   │   └── requirements.txt              # gspread dependency
│   │
│   └── start-invoice-step-function/
│       └── lambda_function.py            # S3 trigger → starts the Step Function
│
└── policies/
    ├── lambdas/
    │   ├── start-textract-expense-policy.json
    │   ├── process-textract-expense-policy.json
    │   ├── invoice-logger-sheets-policy.json
    │   ├── start-invoice-basic-execution-policy.json
    │   └── start-invoice-step-function-policy.json
    │
    └── step-function/
        ├── invoke-lambdas-policy.json    # Allows state machine to invoke Lambdas
        └── xray-policy.json             # X-Ray tracing permissions
```

---

## Lambda Functions

### 1. `start-textract-expense`

**Purpose:** Handles communication with the Amazon Textract API for expense analysis.

This Lambda serves two roles controlled by an `action` parameter:

| Action  | Description |
|---------|-------------|
| `START` | Calls `StartExpenseAnalysis` on a PDF stored in S3 and returns a `JobId`. |
| `CHECK` | Calls `GetExpenseAnalysis` with a `JobId` and returns the current job status. |

**Environment Variables:**
- `RAW_BUCKET` – The S3 bucket where raw invoice PDFs are uploaded.
- `AWS_REGION` – AWS region (defaults to `us-west-1`).

---

### 2. `process-textract-expense`

**Purpose:** Retrieves completed Textract results, normalizes them into a structured format, and writes output files to S3.

**What it does:**
- Paginates through all Textract expense analysis pages.
- Extracts summary fields: invoice ID, date, due date, total, PO number, tax, and freight.
- Extracts vendor info: name, address, and tax ID.
- Extracts receiver/billing/shipping info.
- Parses all line items with per-field confidence scores.
- Generates warning messages for any required fields that are missing.
- Writes two output files to S3:
  - `processed/{JobId}/result.json` – Full normalized JSON payload.
  - `processed/{JobId}/line_items.csv` – Line items in CSV format.

**Environment Variables:**
- `OUTPUT_BUCKET` – The S3 bucket to write processed results to.
- `OUTPUT_PREFIX` – The S3 key prefix for output files (defaults to `processed`).

---

### 3. `invoice-logger-sheets`

**Purpose:** Reads the processed invoice JSON from S3 and appends a summary row to a Google Sheet for tracking.

**What it does:**
- Downloads the `result.json` produced by the process Lambda.
- Extracts key fields: vendor name, invoice ID, total, tax, freight, PO number, and line item count.
- Retrieves Google Sheets API credentials from AWS Secrets Manager.
- Appends a row to the configured Google Sheet with columns:
  `Year | Month | Day | Vendor | Invoice ID | Total | Tax | Freight | Item Count | PO Number | Match Category | S3 Link`

**Environment Variables:**
- `GOOGLE_SHEET_ID` – The ID of the target Google Sheet.
- `SECRET_NAME` – The Secrets Manager secret containing Google service account credentials.

**Dependencies:** `gspread` (see `requirements.txt`).

---

### 4. `start-invoice-step-function`

**Purpose:** Acts as the S3 event trigger that kicks off the entire pipeline.

**What it does:**
- Listens for S3 `PutObject` events on the invoice bucket.
- Filters for files uploaded to the `raw/` prefix only (ignores other prefixes like `emails/`).
- Starts an execution of the Invoice Analyzer Step Function, passing the bucket name and S3 key as input.

**Environment Variables:**
- `STATE_MACHINE_ARN` – The ARN of the Invoice Analyzer Step Function.

---

## Step Function Workflow

The state machine (`invoice-analyzer.asl.json`) orchestrates the full pipeline:

1. **StartTextractJob** – Invokes `start-textract-expense` with `action: START`.
2. **InitPoll** – Initializes a polling counter at `0`.
3. **Wait10Seconds** – Pauses for 10 seconds between status checks.
4. **CheckJobStatus** – Invokes `start-textract-expense` with `action: CHECK`.
5. **IsJobDone?** – Routes based on Textract job status:
   - `SUCCEEDED` / `PARTIAL_SUCCESS` → proceed to extraction.
   - `FAILED` → go to failure handler.
   - Otherwise → increment attempt counter and loop.
6. **BumpAttempt / TooManyAttempts?** – Increments the poll counter. If ≥ 90 attempts (~15 minutes), times out.
7. **ExtractFinalJSON** – Invokes `process-textract-expense` to normalize and save results.
8. **SAPMatchLogic** – Placeholder for SAP 3-way match logic (currently a mock Pass state).
9. **LogToGoogleSheets** – Invokes `invoice-logger-sheets` to log the results.

**Error States:**
- `HandleFailure` – Textract job returned `FAILED`.
- `HandleTimeout` – Polling exceeded the 90-attempt limit.

---

## Setup

### Prerequisites

- AWS account with access to Lambda, Step Functions, S3, Textract, Secrets Manager, and CloudWatch.
- A Google Cloud service account with Sheets API access.
- The Google service account credentials JSON stored in AWS Secrets Manager.

### Configuration

1. Copy `.env.example` to `.env` and fill in your actual values:

   ```bash
   cp .env.example .env
   ```

2. Create an S3 bucket with a `raw/` prefix for uploading invoices.

3. Configure an S3 event notification on the bucket to trigger the `start-invoice-step-function` Lambda when objects are created under `raw/`.

4. Deploy each Lambda function with its corresponding IAM policy from the `policies/lambdas/` directory.

5. Create the Step Function using `state-machine/invoice-analyzer.asl.json` and attach the policies from `policies/step-function/`.

6. Store your Google service account JSON in AWS Secrets Manager under the name specified in your `SECRET_NAME` environment variable.

---

## IAM Policies

All IAM policies are in the `policies/` directory with placeholder values (`123456789012`, `your-invoice-bucket`, etc.). Replace these with your actual AWS account ID, bucket names, and resource ARNs before deploying.

| Policy File | Attached To | Key Permissions |
|---|---|---|
| `start-textract-expense-policy.json` | Lambda #1 role | Textract, S3 read, KMS, CloudWatch |
| `process-textract-expense-policy.json` | Lambda #2 role | Textract read, S3 write, CloudWatch |
| `invoice-logger-sheets-policy.json` | Lambda #3 role | S3 read, Secrets Manager, CloudWatch |
| `start-invoice-basic-execution-policy.json` | Lambda #4 role | CloudWatch logging |
| `start-invoice-step-function-policy.json` | Lambda #4 role | Step Functions start execution |
| `invoke-lambdas-policy.json` | Step Function role | Invoke all three pipeline Lambdas |
| `xray-policy.json` | Step Function role | X-Ray tracing |

---

## Security Notes

- All ARNs, account IDs, bucket names, and secret names in this repo are **placeholders**.
- Real credentials should **never** be committed — use environment variables and Secrets Manager.
- See `.env.example` for the full list of configurable values.
