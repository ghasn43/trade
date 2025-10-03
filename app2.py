# app.py — Supplier sourcing app: URL intake (paste or CSV), on-domain scraping, RFQ, CIF normalization, optional auto-email
# Tip: run on Windows
#   python -m venv .venv
#   .\.venv\Scripts\python -m pip install --upgrade pip
#   .\.venv\Scripts\python -m pip install streamlit httpx beautifulsoup4 lxml pandas xlsxwriter
#   .\.venv\Scripts\streamlit run app.py

import os
import re
import io
import math
import time
import ssl
import json
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
from urllib.parse import urljoin

import httpx
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st
import smtplib
from email.message import EmailMessage

# -------------------------- Page config --------------------------
st.set_page_config(page_title="Supplier Sourcing Studio — CIF Basra", layout="wide")

# -------------------------- Defaults tailored to your brief --------------------------
DEFAULT_PRODUCT_TEXT = (
    "Filters & fluids: engine oil, air, fuel & cabin filters; engine oil & coolant"
)
DEFAULT_DESTINATION = "Basra, Iraq (Umm Qasr Port)"
DEFAULT_INCOTERM = "CIF"
DEFAULT_BUDGET_USD = 300_000
DEFAULT_QTY_UNIT = "assortment"
DEFAULT_QTY = 1
DEFAULT_HS_CODES = [
    "8421.23 (oil/fuel filters for ICE)",
    "8421.31 (intake air filters for ICE)",
    "2710.19 (lubricating oils, preparations)",
    "3820.00 (anti-freeze / coolant preparations)",
]

URLS_SNAPSHOT_PATH = "urls_last.csv"  # local snapshot so your uploaded/pasted URLs survive refresh

# Bootstrap session storage for URLs
if "urls_from_csv" not in st.session_state:
    st.session_state["urls_from_csv"] = []
    if os.path.exists(URLS_SNAPSHOT_PATH):
        try:
            _dfu0 = pd.read_csv(URLS_SNAPSHOT_PATH)
            if "url" in _dfu0.columns:
                st.session_state["urls_from_csv"] = _dfu0["url"].dropna().astype(str).tolist()
        except Exception:
            pass

# -------------------------- Utilities (scraper + helpers) --------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UA = "Mozilla/5.0 (compatible; SourcingStudio/1.0)"

async def fetch_html(url: str, client: httpx.AsyncClient) -> str:
    r = await client.get(url, timeout=20, headers={"User-Agent": UA}, follow_redirects=True)
    r.raise_for_status()
    return r.text

async def extract_contacts_from_site(start_url: str, max_pages: int = 6) -> Dict[str, Any]:
    """
    Very conservative mini-crawler:
      - Starts at given URL, then prioritizes links whose href includes: contact/about/impressum/company
      - Extracts visible emails (including mailto:) and phone numbers
      - Stays shallow (max_pages) and polite (small delay)
    """
    seen = set()
    to_visit = [start_url]
    found_emails, found_phones = set(), set()
    name_guess = ""

    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}) as client:
        while to_visit and len(seen) < max_pages:
            url = to_visit.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                html = await fetch_html(url, client)
            except Exception:
                continue

            soup = BeautifulSoup(html, "lxml")

            # Company name heuristic
            if not name_guess:
                title = soup.find("title")
                h1 = soup.find("h1")
                name_guess = (h1.get_text(strip=True) if h1 else (title.get_text(strip=True) if title else start_url))[:200]

            # Emails (mailto + visible)
            for a in soup.select('a[href^="mailto:"]'):
                email = a.get("href", "").replace("mailto:", "").split("?")[0].strip()
                if EMAIL_RE.fullmatch(email):
                    found_emails.add(email)
            visible_text = soup.get_text(" ", strip=True)
            for m in EMAIL_RE.findall(visible_text):
                found_emails.add(m)

            # Phones (simple pattern)
            for m in re.findall(r"\+?\d[\d\s().-]{7,}\d", visible_text):
                found_phones.add(m.strip())

            # Enqueue likely contact/about pages (stay on same site depth-wise via urljoin)
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if not href or href.startswith("#"):
                    continue
                if any(k in href.lower() for k in ["contact", "about", "impressum", "company"]):
                    full = urljoin(url, href)
                    if full not in seen and full not in to_visit:
                        to_visit.append(full)

            await asyncio.sleep(0.3)  # polite throttle

    return {
        "name": name_guess or start_url,
        "website": start_url,
        "emails": sorted(found_emails),
        "phones": sorted(found_phones),
        "pages_scanned": len(seen),
    }

def normalize_quote(q: Dict[str, Any], fx_to_usd: Dict[str, float], target_incoterm: str = "CIF") -> Dict[str, Any]:
    cur = str(q.get("currency", "USD")).upper()
    rate = float(fx_to_usd.get(cur, 1.0)) or 1.0
    unit_usd = float(q.get("unit_price", 0.0)) / rate
    qty = max(float(q.get("qty", 1.0)), 1.0)
    freight = float(q.get("freight_est", 0.0))
    insurance = float(q.get("insurance_est", 0.0))
    inc = str(q.get("incoterm", "EXW")).upper()
    target = target_incoterm.upper()

    if inc in ("EXW", "FOB") and target == "CIF":
        delivered = unit_usd + (freight + insurance) / qty
        inc_out = "CIF"
    elif inc == "CIF":
        delivered = unit_usd
        inc_out = "CIF"
    else:
        delivered = unit_usd
        inc_out = inc

    q2 = dict(q)
    q2["target_incoterm"] = inc_out
    q2["delivered_unit_usd"] = round(delivered, 6)
    return q2

def score_supplier(row: pd.Series) -> float:
    price = float(row.get("delivered_unit_usd", 0) or 0)
    lead = float(row.get("lead_time_days", 0) or 0)
    email_bonus = 0.05 if row.get("email_present", False) else 0.0
    price_comp = 1.0 / (1.0 + price)
    lead_comp = 1.0 / (1.0 + lead)
    return round(0.7 * price_comp + 0.3 * lead_comp + email_bonus, 4)

def rfq_template(to_name: str, product: str, budget: int, hs_list: list[str], deadline: str) -> str:
    return f"""Subject: RFQ — Filters & Fluids for Iraq — CIF Basra (Umm Qasr) — Budget USD {budget:,}

Dear {to_name or 'Sales Team'},

We are sourcing the following for delivery CIF Basra (Umm Qasr Port), Iraq:
• Scope: {product}
• HS (indicative): {', '.join(hs_list)}
• Quantity / Budget: Aggregate budget USD {budget:,} (assortment basis)
• Requested lead time: {deadline}

Kindly quote:
1) Product list/specs, brand/grade, and HS code used
2) Incoterm & port of loading; for EXW/FOB please indicate available ocean freight & insurance to CIF Basra
3) Unit price, currency, and MOQ; packing details
4) Production lead time and validity of offer
5) Certifications (ISO/CE/RoHS) and origin

Please reply to this email with your PDF quotation and datasheets. Thank you.

Best regards,
Experts Group
"""

# --- email helpers (optional auto-send) ---
def build_email(to_email: str, subject: str, body: str, from_name: str, from_email: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg.set_content(body)
    return msg

def send_via_smtp(msg: EmailMessage, host: str, port: int, user: str, password: str, use_tls: bool = True):
    if use_tls:
        with smtplib.SMTP(host, port) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as server:
            server.login(user, password)
            server.send_message(msg)

# -------------------------- Sidebar: inputs --------------------------
st.sidebar.header("Global Inputs")
product = st.sidebar.text_area("Product scope", DEFAULT_PRODUCT_TEXT, height=100)
destination = st.sidebar.text_input("Destination (port/city)", DEFAULT_DESTINATION)
incoterm = st.sidebar.selectbox("Target Incoterm", ["CIF", "FOB", "EXW", "DDP"], index=0, help="Normalization target for quotes")
budget = st.sidebar.number_input("Budget (USD)", min_value=0, value=DEFAULT_BUDGET_USD, step=10_000)
qty = st.sidebar.number_input("Aggregate Quantity (units or assortments)", min_value=1, value=DEFAULT_QTY)
unit = st.sidebar.text_input("Quantity Unit", DEFAULT_QTY_UNIT)
def_deadline = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")
rfq_deadline = st.sidebar.text_input("Requested lead time / needed by (YYYY-MM-DD)", def_deadline)

with st.sidebar.expander("Email setup (for auto-sending)", expanded=False):
    mail_mode = st.radio("Provider", ["SMTP"], index=0)
    from_name = st.text_input("Your name (From)", "Experts Group")
    from_email = st.text_input("Your email (From)", "you@yourdomain.com")
    smtp_host = st.text_input("SMTP host", "smtp.office365.com")
    smtp_port = st.number_input("SMTP port", 1, 65535, 587)
    smtp_user = st.text_input("SMTP username", from_email)
    smtp_pass = st.text_input("SMTP password / app password", type="password")
    use_tls = st.checkbox("Use STARTTLS", True)
    per_minute = st.slider("Max emails per minute (rate limit)", 1, 60, 12,
                           help="Be polite. 12/min ≈ 5 sec between emails.")
    test_mode = st.checkbox("Dry-run (don’t actually send, just log)", True)

st.title("🔎 Supplier Sourcing Studio — Filters & Fluids → CIF Basra")
st.caption("Paste or upload supplier URLs → scrape on-domain contacts → compose RFQs → normalize quotes to CIF.")

# -------------------------- Section: Supplier discovery --------------------------
st.subheader("1) Supplier discovery & contacts")
left, right = st.columns([1,1], gap="large")

# Right: manual suppliers + CSV upload (with persistence snapshot)
with right:
    st.markdown("**Manual suppliers** (optional)")
    manual_df = st.data_editor(
        pd.DataFrame([{"name": "—", "website": "", "email": "", "country": ""}]),
        num_rows="dynamic",
        use_container_width=True,
        key="manual_suppliers",
    )

    st.markdown("---")
    st.markdown("### Or upload a CSV of URLs")
    file_urls = st.file_uploader(
        "CSV with a 'url' column (or single-column of URLs)",
        type=["csv"],
        key="urls_csv_uploader"
    )
    if file_urls is not None:
        try:
            dfu = pd.read_csv(file_urls)
            if "url" in dfu.columns:
                urls_from_csv_now = dfu["url"].dropna().astype(str).tolist()
            else:
                urls_from_csv_now = dfu.iloc[:, 0].dropna().astype(str).tolist()
            # persist in session
            st.session_state["urls_from_csv"] = urls_from_csv_now
            # snapshot to disk (survives refresh)
            pd.DataFrame({"url": urls_from_csv_now}).to_csv(URLS_SNAPSHOT_PATH, index=False, encoding="utf-8")
            st.success(f"Loaded {len(urls_from_csv_now)} URLs from CSV and saved to {URLS_SNAPSHOT_PATH}.")
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    st.caption(f"URLs currently loaded: **{len(st.session_state.get('urls_from_csv', []))}**")

# Left: paste URLs and kick off scan
with left:
    st.markdown("**Seed websites** (one per line; the app scans Contact/About pages for visible emails/phones)")
    prefill_lines = "\n".join(st.session_state.get("urls_from_csv", [])[:50])  # show first 50 for readability
    seed_sites = st.text_area(
        "One URL per line",
        (prefill_lines or """
https://www.example-filter-factory.com
https://www.example-lubes-producer.com
https://www.example-auto-parts-wholesale.com
""").strip(),
        height=160,
    )
    max_pages = st.slider("Max pages per site to scan", 1, 12, 6)
    btn_scan = st.button("Scan sites for emails", type="primary")

# Run scan
if btn_scan:
    urls = [u.strip() for u in seed_sites.splitlines() if u.strip()]
    extra = st.session_state.get("urls_from_csv", [])
    if extra:
        urls += extra
    urls = list(dict.fromkeys(urls))  # de-dup keep order

    results: List[Dict[str, Any]] = []
    if urls:
        st.info("Polite mode: this demo only opens the URLs you provide; please respect each site’s Terms.")
        with st.spinner("Scanning sites for visible emails…"):
            async def run_all():
                tasks = [extract_contacts_from_site(u, max_pages=max_pages) for u in urls]
                return await asyncio.gather(*tasks, return_exceptions=True)
            out = asyncio.run(run_all())
            for item in out:
                if isinstance(item, dict):
                    results.append(item)

    df_scan = pd.DataFrame(results)
    if not df_scan.empty:
        df_scan["emails"] = df_scan["emails"].apply(lambda x: ", ".join(x) if isinstance(x, list) else str(x))
        df_scan["phones"] = df_scan["phones"].apply(lambda x: ", ".join(x) if isinstance(x, list) else str(x))
        st.success(f"Found contacts on {len(df_scan)} site(s).")
        st.dataframe(df_scan, use_container_width=True)
        st.session_state["scan_results"] = df_scan
        # snapshot contacts too
        try:
            df_scan.to_csv("contacts_latest.csv", index=False, encoding="utf-8")
            st.caption("Contacts also saved to contacts_latest.csv")
        except Exception as e:
            st.warning(f"Could not save contacts CSV: {e}")
    else:
        st.warning("No visible emails/phones found. Try other pages, upload more URLs, or add suppliers manually.")

# -------------------------- Section: RFQ composer --------------------------
st.subheader("2) Compose personalized RFQ email (copy & send from your email client)")

# Gather suppliers list for preview (from scan + manual)
combined_suppliers = []
if "scan_results" in st.session_state and isinstance(st.session_state["scan_results"], pd.DataFrame):
    for _, r in st.session_state["scan_results"].iterrows():
        combined_suppliers.append({
            "name": r.get("name") or r.get("website") or "Supplier",
            "website": r.get("website", ""),
            "email": r.get("emails", ""),
        })
if isinstance(manual_df, pd.DataFrame):
    for _, r in manual_df.iterrows():
        if any([r.get("name"), r.get("website"), r.get("email")]):
            combined_suppliers.append({
                "name": r.get("name") or "Supplier",
                "website": r.get("website", ""),
                "email": r.get("email", ""),
            })
if not combined_suppliers:
    combined_suppliers = [{"name": "Supplier", "website": "", "email": ""}]

col_a, col_b = st.columns([1,1])
with col_a:
    sel_supplier = st.selectbox("Supplier for preview", [s.get("name", "Supplier") for s in combined_suppliers], index=0)
with col_b:
    sel_email = ""
    for s in combined_suppliers:
        if s.get("name") == sel_supplier:
            sel_email = s.get("email", "")
            break
    st.text_input("Email (for your reference)", sel_email)

rfq_preview = rfq_template(sel_supplier, product, int(budget), DEFAULT_HS_CODES, rfq_deadline)
st.code(rfq_preview)


# -------------------------- Section: Quote intake & normalization --------------------------
st.subheader("3) Input quotes and normalize to CIF Basra")

st.markdown("Upload a CSV of quotes (columns: supplier,currency,unit_price,moq,lead_time_days,incoterm,ship_from,freight_est,insurance_est,qty,notes) or edit below.")
upload = st.file_uploader("Upload quotes CSV", type=["csv"], key="quotes_upl")

if upload is not None:
    try:
        quotes_df = pd.read_csv(upload)
    except Exception as e:
        st.error(f"Could not read CSV: {e}")
        quotes_df = pd.DataFrame()
else:
    seed_supplier = (combined_suppliers[0]["name"] if combined_suppliers else "Supplier A")
    quotes_df = pd.DataFrame([
        {"supplier": seed_supplier, "currency": "USD", "unit_price": 2.85, "moq": 100, "lead_time_days": 20, "incoterm": "FOB", "ship_from": "Shanghai", "freight_est": 950, "insurance_est": 45, "qty": 5000, "notes": "Oil/fuel filters mix"},
        {"supplier": "Supplier B", "currency": "CNY", "unit_price": 16.0, "moq": 50, "lead_time_days": 15, "incoterm": "EXW", "ship_from": "Guangzhou", "freight_est": 1100, "insurance_est": 60, "qty": 1000, "notes": "Engine oil 5W-30"},
        {"supplier": "Supplier C", "currency": "USD", "unit_price": 3.2, "moq": 200, "lead_time_days": 28, "incoterm": "CIF", "ship_from": "Jebel Ali", "freight_est": 0, "insurance_est": 0, "qty": 4000, "notes": "Air/cabin filters"},
    ])

quotes_df = st.data_editor(quotes_df, num_rows="dynamic", use_container_width=True, key="quotes_editor")

# FX table (editable)
st.markdown("**FX rates to USD (editable)**")
fx_df = pd.DataFrame({"currency": ["USD", "CNY", "EUR", "AED"], "rate_to_usd": [1.0, 7.15, 0.92, 3.6725]})
fx_df = st.data_editor(fx_df, num_rows="dynamic", use_container_width=True, key="fx_editor")
fx_map = {r.currency.upper(): float(r.rate_to_usd) for r in fx_df.itertuples(index=False)}

# Normalize + score
if not quotes_df.empty:
    norm_rows = []
    # quick map: name -> email presence
    email_presence = {}
    for s in combined_suppliers:
        nm = s.get("name") or "Supplier"
        has_email = bool(s.get("email"))
        email_presence[nm] = email_presence.get(nm, False) or has_email

    for r in quotes_df.to_dict(orient="records"):
        row = normalize_quote(r, fx_map, target_incoterm=incoterm)
        row["email_present"] = bool(email_presence.get(row.get("supplier"), False))
        norm_rows.append(row)

    norm_df = pd.DataFrame(norm_rows)
    if "delivered_unit_usd" in norm_df:
        norm_df["score"] = norm_df.apply(score_supplier, axis=1)
        norm_df = norm_df.sort_values(["delivered_unit_usd", "lead_time_days"]).reset_index(drop=True)

    st.markdown("### Normalized quotes (to target Incoterm)")
    st.dataframe(norm_df, use_container_width=True)

    # Budget coverage estimate (rough)
    avg_unit = norm_df["delivered_unit_usd"].mean() if not norm_df.empty else math.nan
    if avg_unit and avg_unit > 0:
        est_units = int(budget / avg_unit)
        st.info(f"**Budget coverage:** At average delivered unit cost ${avg_unit:,.2f}, USD {budget:,.0f} buys about **{est_units:,} units** (assortment basis).")

    # Export
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as xw:
        quotes_df.to_excel(xw, index=False, sheet_name="Raw Quotes")
        norm_df.to_excel(xw, index=False, sheet_name="Normalized")
        fx_df.to_excel(xw, index=False, sheet_name="FX")
    st.download_button(
        "⬇️ Download Excel (quotes + normalized)",
        data=out.getvalue(),
        file_name="quotes_cif_basra.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# -------------------------- Section: Send RFQs (optional automation) --------------------------
st.subheader("4) Send RFQs by email (automated)")

def collect_recipients() -> list[tuple[str, str]]:
    recips = []
    # From scan results
    if "scan_results" in st.session_state and isinstance(st.session_state["scan_results"], pd.DataFrame):
        for _, r in st.session_state["scan_results"].iterrows():
            nm = r.get("name") or r.get("website") or "Supplier"
            emails_str = r.get("emails", "")
            if isinstance(emails_str, str) and emails_str.strip():
                for em in [e.strip() for e in emails_str.split(",")]:
                    if EMAIL_RE.fullmatch(em):
                        recips.append((nm, em))
    # From manual list
    if isinstance(manual_df, pd.DataFrame):
        for _, r in manual_df.iterrows():
            nm = r.get("name") or "Supplier"
            em = (r.get("email") or "").strip()
            if em and EMAIL_RE.fullmatch(em):
                recips.append((nm, em))
    # de-dup by email
    uniq = {}
    for nm, em in recips:
        uniq[em.lower()] = (nm, em)
    return list(uniq.values())

recipients = collect_recipients()
st.write(f"Discovered **{len(recipients)}** unique recipient emails.")

subject_preview = f"RFQ — Filters & Fluids for Iraq — CIF Basra (Umm Qasr) — Budget USD {budget:,}"
if recipients:
    st.markdown("**Preview (first recipient):**")
    st.code(build_email(
        recipients[0][1],
        subject_preview,
        rfq_template(recipients[0][0], product, int(budget), DEFAULT_HS_CODES, rfq_deadline),
        from_name,
        from_email
    ).as_string())

agree = st.checkbox("I confirm I have permission to contact these suppliers and will include an opt-out.", value=False)
col_send1, col_send2 = st.columns([1,1])
with col_send1:
    btn_send = st.button(f"Send RFQs to {len(recipients)} suppliers")
with col_send2:
    st.caption("Sending mode: " + ("Dry-run (log only)" if test_mode else "SMTP"))

if btn_send:
    if not recipients:
        st.warning("No valid recipient emails found.")
    elif not agree:
        st.warning("Please confirm permission/opt-out checkbox.")
    else:
        sent, errors = 0, 0
        log = st.empty()
        for i, (nm, em) in enumerate(recipients, start=1):
            try:
                msg = build_email(
                    to_email=em,
                    subject=subject_preview,
                    body=rfq_template(nm, product, int(budget), DEFAULT_HS_CODES, rfq_deadline),
                    from_name=from_name,
                    from_email=from_email,
                )
                if not test_mode:
                    send_via_smtp(msg, smtp_host, int(smtp_port), smtp_user, smtp_pass, use_tls=use_tls)
                sent += 1
                log.write(f"Sent {sent}/{len(recipients)} to {em}")
                # rate limit
                if per_minute > 0:
                    time.sleep(60.0 / per_minute)
            except Exception as e:
                errors += 1
                log.write(f"❌ Error sending to {em}: {e}")

        if test_mode:
            st.success(f"Dry-run complete. Would have sent {len(recipients)} emails.")
        else:
            st.success(f"Done. Sent {sent} email(s), {errors} error(s).")

# -------------------------- Tips --------------------------
with st.expander("⚖️ Compliance & sourcing tips", expanded=False):
    st.markdown(
        """
- Check each site’s Terms and robots.txt. Where scraping is not allowed, add suppliers manually or use official APIs.
- Marketplaces often forbid scraping; use on-platform messaging or official exports.
- For CIF Basra: confirm discharge terminal (Umm Qasr North/South or Al Basrah) and align insurance scope.
- Validate brands/specs (e.g., API/ACEA for engine oil); request CoA/CoO and run vendor checks before award.
        """
    )

st.caption("v1.3 — Paste/Upload URLs → Scan contacts → RFQ → CIF normalization → Optional auto-email.")
