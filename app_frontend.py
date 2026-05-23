import streamlit as st
import requests
import pandas as pd
import io
import json

st.set_page_config(page_title="AuditFlow - GST Reconciler", layout="wide")

# ---- Styling ----
st.markdown("""
<style>
    .metric-card { background: #f8f9fa; border-radius: 8px; padding: 12px; border-left: 4px solid #0d6efd; }
    .warning-box { background: #fff3cd; border-left: 4px solid #ffc107; padding: 10px; border-radius: 4px; margin: 4px 0; font-size: 0.85rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
</style>
""", unsafe_allow_html=True)

st.title("💼 AuditFlow: GST Reconciliation Portal")
st.caption("Compare your purchase register against GSTR-2B and surface ITC discrepancies instantly.")

# ---- Upload Section ----
col1, col2 = st.columns(2)
with col1:
    st.subheader("1. Upload Files")
    books_file  = st.file_uploader("Client Purchase Register (.xlsx)", type=["xlsx"])
    gstr2b_file = st.file_uploader("GSTR-2B Payload (.json)",          type=["json"])

with col2:
    st.subheader("2. How It Works")
    st.info("""
    **Column Mapping** — Handles 'Supplier GSTIN', 'Inv No', and tax column variations automatically.

    **GSTIN Validation** — Flags malformed GSTINs and duplicate invoices before processing.

    **Fuzzy Matching** — Resolves `/`, `-`, and case differences in invoice numbers.

    **Variance Buffer** — Gaps under ₹1 are treated as rounding; ₹1–100 flagged as minor mismatches.
    """)


# ---- Row Highlighter ----
def highlight_rows(row):
    status = row.get('Status', '')
    if status == 'Matched':
        return ['background-color: #d4edda; color: #155724'] * len(row)
    elif 'Missing' in str(status):
        return ['background-color: #f8d7da; color: #721c24'] * len(row)
    elif 'Unclaimed' in str(status):
        return ['background-color: #fff3cd; color: #856404'] * len(row)
    elif 'Mismatch' in str(status):
        return ['background-color: #fde8d8; color: #7d3c0e'] * len(row)
    return [''] * len(row)


# ---- Main Action ----
if books_file and gstr2b_file:
    if st.button("Run Reconciliation Engine", type="primary", use_container_width=True):
        with st.spinner("Validating data and running reconciliation..."):

            files = {
                'books':  (books_file.name,  books_file.getvalue(),  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                'gstr2b': (gstr2b_file.name, gstr2b_file.getvalue(), 'application/json'),
            }

            try:
                response = requests.post("http://127.0.0.1:8000/reconcile/", files=files)

                if response.status_code == 200:
                    st.success("Reconciliation complete!")

                    # Parse summary + warnings from response headers
                    summary  = json.loads(response.headers.get("X-Summary",  "{}"))
                    warnings = json.loads(response.headers.get("X-Warnings", "[]"))

                    df_full = pd.read_excel(io.BytesIO(response.content), sheet_name='Recon Detail')

                    # ---- Validation Warnings Panel ----
                    if warnings:
                        with st.expander(f"⚠️ {len(warnings)} Data Warning(s) — Review Before Filing", expanded=True):
                            for w in warnings:
                                st.markdown(f'<div class="warning-box">⚠️ {w}</div>', unsafe_allow_html=True)

                    # ---- KPI Metrics ----
                    st.subheader("ITC Summary")
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("Total Invoices (Books)",   summary.get('total_invoices_books', '—'))
                    m2.metric("Total Invoices (GSTR-2B)", summary.get('total_invoices_2b',    '—'))
                    m3.metric("✅ Matched",               summary.get('matched_count',         '—'))
                    m4.metric("⚠️ Actionable Items",      summary.get('mismatch_count',        '—'))
                    m5.metric("🔴 ITC at Risk (₹)",       f"₹{summary.get('itc_at_risk', 0):,.2f}")

                    # ---- Tabs ----
                    tab1, tab2, tab3 = st.tabs(["📋 Full Reconciliation", "🏢 Supplier Drill-Down", "📊 Status Breakdown"])

                    with tab1:
                        # Filter controls
                        fc1, fc2 = st.columns([2, 1])
                        with fc1:
                            status_filter = st.multiselect(
                                "Filter by Status",
                                options=df_full['Status'].unique().tolist(),
                                default=df_full['Status'].unique().tolist()
                            )
                        with fc2:
                            gstin_search = st.text_input("Search GSTIN", placeholder="e.g. 27AAAAA...")

                        filtered = df_full[df_full['Status'].isin(status_filter)]
                        if gstin_search:
                            gstin_col = 'gstin' if 'gstin' in filtered.columns else 'gstin_2b'
                            filtered = filtered[
                                filtered[gstin_col].astype(str).str.contains(gstin_search.upper(), na=False)
                            ]

                        st.caption(f"Showing {len(filtered)} of {len(df_full)} records")
                        styled = filtered.style.apply(highlight_rows, axis=1)
                        st.dataframe(styled, use_container_width=True, height=420)

                    with tab2:
                        # Per-supplier summary
                        gstin_col = 'gstin' if 'gstin' in df_full.columns else 'gstin_2b'
                        if gstin_col in df_full.columns:
                            supplier_summary = (
                                df_full.groupby(gstin_col)
                                .agg(
                                    Total_Invoices=('Status', 'count'),
                                    Matched=('Status', lambda x: (x == 'Matched').sum()),
                                    Issues=('Status', lambda x: (x != 'Matched').sum()),
                                    ITC_at_Risk=('ITC_at_risk', 'sum') if 'ITC_at_risk' in df_full.columns else ('Status', 'count')
                                )
                                .reset_index()
                                .sort_values('Issues', ascending=False)
                            )
                            st.dataframe(supplier_summary, use_container_width=True, height=400)
                        else:
                            st.info("GSTIN column not found for supplier grouping.")

                    with tab3:
                        status_counts = df_full['Status'].value_counts().reset_index()
                        status_counts.columns = ['Status', 'Count']
                        if 'ITC_at_risk' in df_full.columns:
                            itc_by_status = df_full.groupby('Status')['ITC_at_risk'].sum().reset_index()
                            itc_by_status.columns = ['Status', 'ITC at Risk (₹)']
                            status_counts = status_counts.merge(itc_by_status, on='Status', how='left').fillna(0)
                        st.dataframe(status_counts, use_container_width=True)

                    # ---- Download ----
                    st.download_button(
                        label="📥 Download Full Audit Report (.xlsx)",
                        data=response.content,
                        file_name=f"GST_Recon_{books_file.name.split('.')[0]}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )

                else:
                    st.error(f"Backend Error: {response.text}")

            except requests.exceptions.ConnectionError:
                st.error("Could not reach the backend. Make sure `app_backend.py` is running on port 8000.")
            except Exception as e:
                st.error(f"Unexpected error: {e}")

else:
    st.info("Upload both files above to begin reconciliation.")