"""Cost of Funds calculator for non-performing loans. Run: streamlit run 01_app.py"""
import datetime as dt
import hashlib
import io
import os

import pandas as pd
import streamlit as st

import cof_core as core

st.set_page_config(page_title="Cost of Funds Calculator", page_icon="🏦", layout="wide")

# --- USER AUTHENTICATION CONFIGURATION ---
# Default password hash here corresponds to 'admin123'
# You can generate new hashes using hashlib.sha256(b"your_password").hexdigest()
USERS = {
    "admin": "240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"
}

def make_hash(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def check_login():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        st.title("🏦 Cost of Funds Calculator")
        st.caption("Please log in to access the calculator.")
        
        col1, col2, col3 = st.columns([1, 1.5, 1])
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            st.subheader("🔐 User Authentication")
            
            with st.form("login_form"):
                # Clean input to remove accidental leading/trailing spaces
                username = st.text_input("Username").strip().lower()
                password = st.text_input("Password", type="password")
                submit_btn = st.form_submit_button("Login", use_container_width=True)
                
                if submit_btn:
                    # Fetch passwords safely from Streamlit secrets
                    passwords = st.secrets.get("passwords", {})
                    
                    # Convert secret keys to lowercase as well for matching
                    passwords_lower = {k.lower(): v for k, v in passwords.items()}
                    
                    if username in passwords_lower and passwords_lower[username] == make_hash(password):
                        st.session_state["authenticated"] = True
                        st.session_state["username"] = username
                        st.rerun()
                    else:
                        st.error("⚠️ Invalid username or password")
        return False
    return True

# --- MAIN APP GATE ---
if check_login():
    st.title("🏦 Cost of Funds Calculator - Non-Performing Loans")
    st.caption("Upload statement of account → store (Name, Date, Balance) → set default / end dates and yearly rates → "
               "cost of funds = Σ (balance × rate × days ÷ day-count basis).")

    # ------------------------------------------------------------------ sidebar: settings
    with st.sidebar:
        st.write(f"👤 Logged in as: **{st.session_state.get('username')}**")
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state["authenticated"] = False
            st.rerun()
            
        st.divider()
        st.header("⚙️ Settings")
        # Use a path inside the app directory so it persists across reruns on the same session.
        # On Streamlit Cloud, the working directory is the repo root.
        db_path = os.path.join(os.path.dirname(__file__), "data", "cost_of_funds.db")
        st.info(f"💾 Database: `cost_of_funds.db` (SQLite)")
        st.divider()
        st.subheader("PDF parser")
        dayfirst = st.checkbox("Dates are day-first (DD/MM/YYYY)", True)
        req_dec = st.checkbox("Amounts always have 2 decimals", True,
                             help="Untick if balances are printed without decimals.")


    @st.cache_resource(show_spinner=False)
    def get_store(path: str) -> core.Store:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return core.Store(path)


    try:
        store = get_store(db_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Database error: {exc}")
        st.stop()
    st.sidebar.success(f"Connected: cost_of_funds.db")

    # ------------------------------------------------------------------ step 1-3: upload, extract, store
    st.header("1️⃣ Upload statement(s) of account")
    files = st.file_uploader("PDF statement(s) - text-based PDFs only (no scans)", type="pdf", accept_multiple_files=True)

    for f in files or []:
        with st.expander(f"📄 {f.name}", expanded=True):
            try:
                ex = core.extract_statement(io.BytesIO(f.getvalue()), dayfirst, req_dec)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read this PDF: {exc}")
                continue
            for w in ex.warnings:
                st.warning(w)
            name = st.text_input("Account / customer name", ex.name or "", key=f"name_{f.name}",
                                 help="Detected from the statement - correct it if needed.")
            st.write(f"Extracted **{len(ex.rows)}** rows from {ex.pages} page(s). Review / edit before saving:")
            edited = st.data_editor(ex.rows, num_rows="dynamic", width="stretch", key=f"ed_{f.name}",
                                    column_config={"txn_date": st.column_config.DateColumn("Date", format="DD-MM-YYYY"),
                                                   "balance": st.column_config.NumberColumn("Balance", format="%.2f")})
            if st.button("💾 Save to database", key=f"save_{f.name}", disabled=not (name.strip() and len(edited))):
                clean = edited.dropna(subset=["txn_date", "balance"])
                n = store.save(name.strip(), f.name, clean)
                st.success(f"Saved {n} rows for '{name.strip()}'.")

    # ------------------------------------------------------------------ step 4: select account + dates
    st.header("2️⃣ Select account and period")
    accounts = store.accounts()
    if not accounts:
        st.info("No data stored yet - upload and save a statement above.")
        st.stop()

    c1, c2 = st.columns([2, 1])
    account = c1.selectbox("Account", accounts)
    data = store.load(account)
    first, last = data["txn_date"].min(), data["txn_date"].max()
    c2.metric("Statement coverage", f"{first:%d-%m-%Y} → {last:%d-%m-%Y}")
    with st.expander(f"Stored rows for '{account}' ({len(data)})"):
        st.dataframe(data.sort_values(["txn_date", "seq"]), width="stretch", hide_index=True)
        if st.button("🗑️ Delete this account's data"):
            store.delete_account(account)
            st.rerun()

    d1, d2, d3 = st.columns(3)
    fmt = "DD/MM/YYYY"
    default_date = d1.date_input("Date of default", value=first, format=fmt, min_value=dt.date(1990, 1, 1),
                                 max_value=dt.date(2100, 12, 31))
    end_date = d2.date_input("Ending date (cost of funds calculated up to)", value=last, format=fmt,
                             min_value=dt.date(1990, 1, 1), max_value=dt.date(2100, 12, 31))
    with d3:
        convention = st.selectbox("Day-count convention", ["Actual/365", "Actual/360", "Actual/Actual"])
    o1, o2, o3 = st.columns(3)
    year_basis = o1.radio("Year basis for rates", ["Calendar year (Jan–Dec)", "Fiscal year (Jul–Jun)"])
    include_end = o2.checkbox("Include ending date in day count", False,
                              help="Off: days = ending date − default date. On: one extra day.")
    use_abs = o3.checkbox("Treat balance as absolute value", True,
                          help="Loan statements often show outstanding as negative / Dr.")
    fiscal = year_basis.startswith("Fiscal")

    # ------------------------------------------------------------------ step 5: yearly rates
    st.header("3️⃣ Yearly cost-of-funds rates")
    if end_date < default_date or (end_date == default_date and not include_end):
        st.error("Ending date must be after the date of default.")
        st.stop()
    labels = core.labels_for_period(default_date, end_date, fiscal, include_end)
    st.session_state.setdefault("rates", {})

    # Simple number_input per year — no double-click needed
    cols = st.columns(min(len(labels), 4))
    rates = {}
    for i, label in enumerate(labels):
        col = cols[i % len(cols)]
        val = col.number_input(
            label,
            min_value=0.0,
            max_value=100.0,
            value=float(st.session_state["rates"].get(label) or 0.0),
            step=0.01,
            format="%.4f",
            key=f"rate_input_{label}",
        )
        st.session_state["rates"][label] = val
        if val > 0:
            rates[label] = val

    # ------------------------------------------------------------------ step 6: calculate
    st.header("4️⃣ Calculate")
    if st.button("▶️ Calculate cost of funds", type="primary"):
        missing = [l for l in labels if l not in rates]
        if missing:
            st.error("Please enter a rate for: " + ", ".join(missing))
        else:
            try:
                sched, summ, warns = core.compute_cost_of_funds(data, default_date, end_date, rates, convention,
                                                             fiscal, include_end, use_abs)
                st.session_state["result"] = dict(sched=sched, summ=summ, warns=warns,
                                                   account=account, default_date=default_date,
                                                   end_date=end_date, convention=convention,
                                                   year_basis=year_basis, include_end=include_end,
                                                   use_abs=use_abs)
            except ValueError as exc:
                st.error(str(exc))
                st.session_state.pop("result", None)

    # -- display results (persists across reruns via session_state) --
    if "result" in st.session_state:
        res = st.session_state["result"]
        sched, summ = res["sched"], res["summ"]
        for w in res["warns"]:
            st.warning(w)
        total = summ["Cost of Funds"].sum()
        m1, m2, m3 = st.columns(3)
        m1.metric("Total cost of funds", f"Rs. {total:,.2f}")
        m2.metric("Total days", f"{int(summ['Days'].sum()):,}")
        m3.metric("Opening balance at default", f"{sched['Balance'].iloc[0]:,.2f}")

        st.subheader("Yearly summary")
        show = summ.copy()
        tot = {"Year": "TOTAL", "Days": show["Days"].sum(),
               "Average Balance": (show["Average Balance"] * show["Days"]).sum() / show["Days"].sum(),
               "Rate %": None, "Cost of Funds": total}
        show = pd.concat([show, pd.DataFrame([tot])], ignore_index=True)
        st.dataframe(show.style.format({"Average Balance": "{:,.2f}", "Cost of Funds": "{:,.2f}",
                                        "Rate %": "{:.4f}", "Days": "{:,.0f}"}, na_rep=""),
                     width="stretch", hide_index=True)

        with st.expander("Detailed period-wise schedule", expanded=False):
            st.dataframe(sched.style.format({"Balance": "{:,.2f}", "Cost of Funds": "{:,.2f}", "Rate %": "{:.4f}",
                                             "From": "{:%d-%m-%Y}", "To": "{:%d-%m-%Y}"}),
                         width="stretch", hide_index=True)

        buf = io.BytesIO()
        params = pd.DataFrame({"Parameter": ["Account", "Date of default", "Ending date", "Day count",
                                             "Year basis", "Include ending date", "Absolute balance"],
                               "Value": [res["account"], f"{res['default_date']:%d-%m-%Y}",
                                         f"{res['end_date']:%d-%m-%Y}", res["convention"],
                                         res["year_basis"], res["include_end"], res["use_abs"]]})
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            show.to_excel(xw, sheet_name="Yearly Summary", index=False)
            sched.to_excel(xw, sheet_name="Schedule", index=False)
            params.to_excel(xw, sheet_name="Parameters", index=False)
        excel_bytes = buf.getvalue()
        st.download_button("⬇️ Download Excel", excel_bytes, f"cost_of_funds_{res['account']}.xlsx",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
