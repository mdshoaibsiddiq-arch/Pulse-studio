import io
import json
import re
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from rapidfuzz import fuzz

app = FastAPI(title="GST Automation Engine API")

# -------------------------------------------------------------------
# GSTIN Validation
# -------------------------------------------------------------------
GSTIN_PATTERN = re.compile(
    r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
)

def is_valid_gstin(gstin: str) -> bool:
    if not isinstance(gstin, str):
        return False
    gstin = gstin.strip().upper()
    return bool(GSTIN_PATTERN.match(gstin))

def validate_client_books(df: pd.DataFrame) -> list:
    warnings = []

    if 'gstin' in df.columns:
        invalid_gstins = df[~df['gstin'].apply(is_valid_gstin)]['gstin'].unique()
        for g in invalid_gstins:
            warnings.append(f"Invalid GSTIN format: '{g}'")

    if 'gstin' in df.columns and 'invoice_no' in df.columns:
        dupes = df[df.duplicated(subset=['gstin', 'invoice_no'], keep=False)]
        if not dupes.empty:
            for _, row in dupes.iterrows():
                warnings.append(
                    f"Duplicate invoice in books: GSTIN={row['gstin']}, Inv={row['invoice_no']}"
                )

    if 'taxable_value' in df.columns:
        bad_vals = df[df['taxable_value'] <= 0]
        for _, row in bad_vals.iterrows():
            warnings.append(
                f"Zero/negative taxable value: Inv={row.get('invoice_no', 'N/A')}"
            )

    for col in ['gstin', 'invoice_no', 'taxable_value']:
        if col in df.columns:
            missing = df[df[col].isna() | (df[col].astype(str).str.strip() == '')]
            if not missing.empty:
                warnings.append(f"Missing '{col}' in {len(missing)} row(s)")

    return warnings


# -------------------------------------------------------------------
# Invoice Matching Helpers
# -------------------------------------------------------------------
def clean_invoice_num(inv_num):
    if pd.isna(inv_num):
        return ""
    cleaned = "".join(e for e in str(inv_num).strip().upper() if e.isalnum())
    return cleaned.lstrip('0')

def is_fuzzy_match(inv1, inv2, threshold=85):
    c1 = clean_invoice_num(inv1)
    c2 = clean_invoice_num(inv2)
    if c1 == c2:
        return True
    return fuzz.token_sort_ratio(c1, c2) >= threshold


# -------------------------------------------------------------------
# Core Reconciliation Engine
# -------------------------------------------------------------------
def process_reconciliation(client_bytes, json_bytes):
    df = pd.read_excel(io.BytesIO(client_bytes))

    rename_dict = {
        'Supplier GSTIN': 'gstin', 'GSTIN of Supplier': 'gstin', 'GSTIN': 'gstin',
        'Invoice Number': 'invoice_no', 'Inv No': 'invoice_no', 'Invoice No': 'invoice_no',
        'Invoice Date': 'invoice_date', 'Inv Date': 'invoice_date',
        'Taxable Value': 'taxable_value', 'CGST': 'cgst', 'SGST': 'sgst', 'IGST': 'igst'
    }
    df.rename(columns={k: v for k, v in rename_dict.items() if k in df.columns}, inplace=True)

    df['gstin'] = df['gstin'].astype(str).str.strip().str.upper()
    df['invoice_no'] = df['invoice_no'].astype(str).str.strip()

    for col in ['taxable_value', 'cgst', 'sgst', 'igst']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        else:
            df[col] = 0.0

    df['total_rec_itc'] = df['cgst'] + df['sgst'] + df['igst']

    warnings = validate_client_books(df)

    # Parse GSTR-2B JSON
    data = json.loads(json_bytes.decode('utf-8'))
    rows = []
    for b2b in data.get('data', {}).get('docdata', {}).get('b2b', []):
        ctin = b2b.get('ctin', '')
        for inv in b2b.get('inv', []):
            inum = inv.get('inum', '')
            idt  = inv.get('idt', '')
            val  = float(inv.get('val', 0))
            cgst = sgst = igst = 0.0
            for itm in inv.get('itms', []):
                d = itm.get('itm_det', {})
                cgst += float(d.get('camt', 0))
                sgst += float(d.get('samt', 0))
                igst += float(d.get('iamt', 0))
            rows.append({
                'gstin_2b':        ctin.strip().upper(),
                'invoice_no_2b':   inum.strip(),
                'invoice_date_2b': idt,
                'cgst_2b':         cgst,
                'sgst_2b':         sgst,
                'igst_2b':         igst,
                'total_2b_itc':    cgst + sgst + igst,
                'taxable_val_2b':  val
            })
    gstr2b_df = pd.DataFrame(rows)

    # Matching
    matched_records = []
    unmatched_2b_indices = set(gstr2b_df.index)

    for _, client_row in df.iterrows():
        gstin  = client_row['gstin']
        inv_no = client_row['invoice_no']

        potential_matches = gstr2b_df[
            (gstr2b_df['gstin_2b'] == gstin) &
            (gstr2b_df.index.isin(unmatched_2b_indices))
        ]

        match_found = False
        for idx_2b, row_2b in potential_matches.iterrows():
            if is_fuzzy_match(inv_no, row_2b['invoice_no_2b']):
                variance = abs(client_row['total_rec_itc'] - row_2b['total_2b_itc'])
                if variance <= 1.0:
                    status = "Matched"
                elif variance <= 100.0:
                    status = "Minor Value Mismatch"
                else:
                    status = "Value Mismatch / Discrepancy"

                matched_records.append({
                    **client_row.to_dict(),
                    **row_2b.to_dict(),
                    'Status':        status,
                    'Variance (Rs)': round(variance, 2),
                    'ITC_at_risk':   round(variance, 2) if status != "Matched" else 0.0
                })
                unmatched_2b_indices.remove(idx_2b)
                match_found = True
                break

        if not match_found:
            matched_records.append({
                **client_row.to_dict(),
                'Status':        'Missing in GSTR-2B',
                'Variance (Rs)': 0.0,
                'ITC_at_risk':   round(client_row['total_rec_itc'], 2)
            })

    for idx_2b in unmatched_2b_indices:
        row_2b = gstr2b_df.loc[idx_2b]
        matched_records.append({
            **row_2b.to_dict(),
            'Status':        'Unclaimed in Books',
            'Variance (Rs)': 0.0,
            'ITC_at_risk':   round(row_2b['total_2b_itc'], 2)
        })

    result_df = pd.DataFrame(matched_records)

    # ITC Summary
    total_itc_books = df['total_rec_itc'].sum()
    matched_df      = result_df[result_df['Status'] == 'Matched']
    mismatch_df     = result_df[~result_df['Status'].isin(['Matched'])]

    summary = {
        'total_invoices_books': int(len(df)),
        'total_invoices_2b':    int(len(gstr2b_df)),
        'matched_count':        int(len(matched_df)),
        'mismatch_count':       int(len(mismatch_df)),
        'total_itc_books':      round(float(total_itc_books), 2),
        'matched_itc':          round(float(matched_df['total_rec_itc'].sum()) if 'total_rec_itc' in matched_df.columns else 0, 2),
        'itc_at_risk':          round(float(result_df['ITC_at_risk'].sum()), 2),
    }

    return result_df, warnings, summary


# -------------------------------------------------------------------
# API Endpoint
# -------------------------------------------------------------------
@app.post("/reconcile/")
async def reconcile_files(books: UploadFile = File(...), gstr2b: UploadFile = File(...)):
    try:
        books_bytes  = await books.read()
        gstr2b_bytes = await gstr2b.read()

        result_df, warnings, summary = process_reconciliation(books_bytes, gstr2b_bytes)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            result_df.to_excel(writer, index=False, sheet_name='Recon Detail')

            summary_rows = [
                {"Metric": "Total Invoices (Books)",   "Value": summary['total_invoices_books']},
                {"Metric": "Total Invoices (GSTR-2B)", "Value": summary['total_invoices_2b']},
                {"Metric": "Matched",                  "Value": summary['matched_count']},
                {"Metric": "Mismatches / Missing",     "Value": summary['mismatch_count']},
                {"Metric": "Total ITC in Books (Rs)",  "Value": summary['total_itc_books']},
                {"Metric": "Matched ITC (Rs)",         "Value": summary['matched_itc']},
                {"Metric": "ITC at Risk (Rs)",         "Value": summary['itc_at_risk']},
            ]
            pd.DataFrame(summary_rows).to_excel(writer, index=False, sheet_name='ITC Summary')

            if warnings:
                pd.DataFrame({"Validation Warnings": warnings}).to_excel(
                    writer, index=False, sheet_name='Data Warnings'
                )

        output.seek(0)

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": "attachment; filename=reconciled_report.xlsx",
                "X-Summary":  json.dumps(summary),
                "X-Warnings": json.dumps(warnings),
                "Access-Control-Expose-Headers": "X-Summary, X-Warnings",
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing Error: {str(e)}")


# Strip out local file creation blocks that crash serverless containers
app = app
    import uvicorn, os

    if not os.path.exists("client_books.xlsx"):
        pd.DataFrame([
            {'Supplier GSTIN': '27AAAAA1111A1Z1', 'Invoice Number': 'INV-2026-001', 'Taxable Value': 10000, 'CGST': 900,  'SGST': 900},
            {'Supplier GSTIN': '27BBBBB2222B2Z2', 'Invoice Number': 'INV/99A',       'Taxable Value': 5000,  'CGST': 450,  'SGST': 450},
            {'Supplier GSTIN': '27CCCCC3333C3Z3', 'Invoice Number': 'INV-105',       'Taxable Value': 12000, 'CGST': 1080, 'SGST': 1080},
        ]).to_excel("client_books.xlsx", index=False)

    if not os.path.exists("gstr2b_api_response.json"):
        dummy = {"data": {"docdata": {"b2b": [
            {"ctin": "27AAAAA1111A1Z1", "inv": [{"inum": "INV-2026-001", "idt": "01-05-2026", "val": 10000, "itms": [{"itm_det": {"camt": 900, "samt": 900}}]}]},
            {"ctin": "27BBBBB2222B2Z2", "inv": [{"inum": "INV99A",       "idt": "12-05-2026", "val": 5000,  "itms": [{"itm_det": {"camt": 450, "samt": 450}}]}]},
        ]}}}
        with open("gstr2b_api_response.json", "w") as f:
            json.dump(dummy, f)

    uvicorn.run(app, host="127.0.0.1", port=8000)
