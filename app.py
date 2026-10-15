"""Personal Finance Dashboard

Upload a bank statement (CSV, Excel or PDF), auto-categorise transactions
with keyword rules stored in categories.json, edit categories inline, and
visualise spending.

Run with:  streamlit run app.py
"""

import json
import re
from pathlib import Path

import pymupdf  # PyMuPDF
import pandas as pd
import plotly.express as px
import streamlit as st

# categories.json lives next to app.py, so it works from any working directory
CATEGORY_FILE = Path(__file__).parent / "categories.json"

DEFAULT_CATEGORIES = {
    "Shopping": ["noon", "amazon", "lulu"],
    "Subscriptions": ["netflix", "spotify", "apple.com/bill", "google *"],
    "Travel": ["uber", "careem", "emirates", "etihad", "airlines"],
    "Insurance": ["insurance", "takaful"],
    "Bank Fees": ["fee", "service charge", "vat"],
    "Utilities": ["dewa", "etisalat", "du telecom", "salik"],
    "Uncategorized": [],
}

# Accepted header names (lower-case) for each logical column
COLUMN_ALIASES = {
    "date": ["date", "transaction date", "txn date", "posting date", "value date"],
    "details": ["details", "description", "narration", "merchant", "particulars",
                "transaction details"],
    "amount": ["amount", "transaction amount", "value"],
    "type": ["debit/credit", "debit_credit", "dr/cr", "type", "transaction type"],
    "currency": ["currency", "ccy"],
}

DATE_FORMATS = ["%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"]

# Patterns for pulling transactions out of the text layer of a PDF statement
PDF_DATE_RE = re.compile(
    r"(\d{1,2}[-/ ](?:[A-Za-z]{3,9}|\d{1,2})[-/ ]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})"
)
PDF_AMOUNT_RE = re.compile(r"(?<![\d.,])[(\-+]?[\d,]+(?:\.\d+)?\)?\s*(?:CR|DR)?$", re.IGNORECASE)


# ----------------------------------------------------------------------------
# Category storage
# ----------------------------------------------------------------------------
def save_categories(categories: dict) -> None:
    with open(CATEGORY_FILE, "w", encoding="utf-8") as f:
        json.dump(categories, f, indent=4, ensure_ascii=False)


def load_categories() -> dict:
    """Load categories.json, creating it with defaults if missing or corrupted."""
    if not CATEGORY_FILE.exists():
        save_categories(DEFAULT_CATEGORIES)
        return {k: list(v) for k, v in DEFAULT_CATEGORIES.items()}

    try:
        with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("categories.json must contain a JSON object")
        data = {
            str(k): [str(x) for x in v] if isinstance(v, list) else []
            for k, v in data.items()
        }
    except (json.JSONDecodeError, ValueError, OSError):
        # Keep the broken file as a backup and start fresh
        try:
            CATEGORY_FILE.replace(CATEGORY_FILE.with_suffix(".json.bak"))
        except OSError:
            pass
        save_categories(DEFAULT_CATEGORIES)
        return {k: list(v) for k, v in DEFAULT_CATEGORIES.items()}

    data.setdefault("Uncategorized", [])
    return data


# ----------------------------------------------------------------------------
# Data cleaning
# ----------------------------------------------------------------------------
def read_csv_safely(uploaded_file) -> pd.DataFrame:
    if uploaded_file.size == 0:
        raise ValueError("The uploaded file is empty.")
    last_error = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding=encoding)
        except UnicodeDecodeError as e:
            last_error = e
        except pd.errors.EmptyDataError:
            raise ValueError("The CSV has no columns or rows to read.")
    raise ValueError(f"Could not decode the file: {last_error}")


def read_excel_safely(uploaded_file) -> pd.DataFrame:
    if uploaded_file.size == 0:
        raise ValueError("The uploaded file is empty.")
    uploaded_file.seek(0)
    try:
        return pd.read_excel(uploaded_file)
    except Exception as e:
        raise ValueError(f"Could not read the Excel file: {e}")


def _parse_pdf_text(text: str) -> pd.DataFrame:
    """Pull Date/Details/Amount rows out of the text layer of a PDF."""
    rows = []
    for line in text.splitlines():
        line = " ".join(line.split())
        date_match = PDF_DATE_RE.search(line)
        if not date_match:
            continue
        amount_match = PDF_AMOUNT_RE.search(line, date_match.end())
        if not amount_match:
            continue
        details = line[date_match.end():amount_match.start()].strip(" .-")
        if not details:
            continue
        amount_text = amount_match.group(0).strip()
        amount = re.sub(r"\s*(?:CR|DR)$", "", amount_text, flags=re.IGNORECASE).strip()
        if amount_text.upper().endswith("DR") and not amount.startswith("-"):
            amount = f"-{amount}"
        rows.append({"Date": date_match.group(0), "Details": details, "Amount": amount})
    return pd.DataFrame(rows, columns=["Date", "Details", "Amount"])


def read_pdf_safely(uploaded_file) -> pd.DataFrame:
    if uploaded_file.size == 0:
        raise ValueError("The uploaded file is empty.")
    uploaded_file.seek(0)
    try:
        with pymupdf.open(stream=uploaded_file.read(), filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        raise ValueError(f"Could not read the PDF: {e}")
    df = _parse_pdf_text(text)
    if df.empty:
        raise ValueError(
            "No transactions were found in the PDF. Upload a text-based bank "
            "statement (scanned/image PDFs are not supported)."
        )
    return df


def read_uploaded_file(uploaded_file) -> pd.DataFrame:
    """Pick the right reader based on the uploaded file's extension."""
    suffix = Path(getattr(uploaded_file, "name", "") or "").suffix.lower()
    if suffix in (".xls", ".xlsx"):
        return read_excel_safely(uploaded_file)
    if suffix == ".pdf":
        return read_pdf_safely(uploaded_file)
    return read_csv_safely(uploaded_file)


def _find_column(columns, key):
    lowered = {c.lower(): c for c in columns}
    for alias in COLUMN_ALIASES[key]:
        if alias in lowered:
            return lowered[alias]
    return None


def _parse_dates(series: pd.Series) -> pd.Series:
    """Try known formats first, then fall back to pandas' mixed-format parser."""
    text = series.astype(str).str.strip()
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(text, format=fmt, errors="coerce")
        if parsed.notna().all():
            return parsed
    try:
        return pd.to_datetime(text, format="mixed", dayfirst=True, errors="coerce")
    except (TypeError, ValueError):  # older pandas without format="mixed"
        return pd.to_datetime(text, dayfirst=True, errors="coerce")


def _to_number(series: pd.Series) -> pd.Series:
    """Handle '1,234.50', 'AED 99', '$12', '-45.10' and '(45.10)' style values."""
    text = series.astype(str).str.strip()
    is_paren_negative = text.str.match(r"^\(.*\)$")
    cleaned = text.str.replace(r"[^0-9.\-]", "", regex=True)
    numbers = pd.to_numeric(cleaned, errors="coerce")
    return numbers.where(~is_paren_negative, -numbers.abs())


def _normalise_type(value) -> str | None:
    v = str(value).strip().lower()
    if v.startswith(("deb", "dr", "withdraw")):
        return "Debit"
    if v.startswith(("cred", "cr", "deposit")):
        return "Credit"
    return None


def clean_data(raw: pd.DataFrame):
    """Return (clean_df, warnings). Raises ValueError for unusable schemas."""
    warnings = []
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    date_col = _find_column(df.columns, "date")
    details_col = _find_column(df.columns, "details")
    amount_col = _find_column(df.columns, "amount")
    type_col = _find_column(df.columns, "type")
    currency_col = _find_column(df.columns, "currency")

    missing = [name for name, col in
               [("Date", date_col), ("Details/Description", details_col), ("Amount", amount_col)]
               if col is None]
    if missing:
        raise ValueError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Columns found: {', '.join(df.columns)}"
        )

    out = pd.DataFrame({
        "Date": _parse_dates(df[date_col]),
        "Details": df[details_col].astype(str).str.strip(),
        "Amount": _to_number(df[amount_col]),
    })
    if currency_col:
        out["Currency"] = df[currency_col].astype(str).str.strip()

    # Debit / Credit: use the column if present, otherwise infer from the sign
    inferred = out["Amount"].apply(lambda x: "Debit" if x < 0 else "Credit")
    if type_col:
        out["Type"] = df[type_col].map(_normalise_type).fillna(inferred)
    else:
        out["Type"] = inferred
        warnings.append("No Debit/Credit column found, so negative amounts were treated as debits.")
    out["Amount"] = out["Amount"].abs()

    bad_rows = out["Date"].isna() | out["Amount"].isna()
    if bad_rows.any():
        warnings.append(f"Skipped {int(bad_rows.sum())} row(s) with an unreadable date or amount.")
        out = out[~bad_rows]

    out = out.reset_index(drop=True)
    if out.empty:
        raise ValueError("No valid transactions were found in the file.")
    return out, warnings


# ----------------------------------------------------------------------------
# Categorisation
# ----------------------------------------------------------------------------
def categorize_transaction(description, categories_dict: dict) -> str:
    """Return the category whose keyword matches the description (longest match wins)."""
    desc = str(description).lower().strip()
    best_category, best_len = "Uncategorized", 0
    for category, keywords in categories_dict.items():
        if category == "Uncategorized":
            continue
        for kw in keywords:
            kw = kw.lower().strip()
            if kw and kw in desc and len(kw) > best_len:
                best_category, best_len = category, len(kw)
    return best_category


def categorize_data(df: pd.DataFrame, categories_dict: dict, only_uncategorized=False) -> pd.DataFrame:
    df = df.copy()
    if "Category" not in df.columns:
        df["Category"] = "Uncategorized"
    mask = (df["Category"] == "Uncategorized") if only_uncategorized else pd.Series(True, index=df.index)
    if mask.any():
        df.loc[mask, "Category"] = df.loc[mask, "Details"].apply(
            lambda d: categorize_transaction(d, categories_dict)
        )
    return df


def apply_edits(edited: pd.DataFrame) -> int:
    """Write edited categories back to session data and save new keyword rules."""
    df = st.session_state.df
    cats = st.session_state.categories
    current = df.loc[edited.index, "Category"]
    changed = edited.index[edited["Category"] != current]

    for idx in changed:
        new_cat = edited.at[idx, "Category"]
        if new_cat not in cats:
            continue
        df.at[idx, "Category"] = new_cat
        keyword = str(df.at[idx, "Details"]).strip().lower()
        if new_cat == "Uncategorized" or not keyword:
            continue
        # a description can only belong to one category
        for name in cats:
            cats[name] = [k for k in cats[name] if k.lower().strip() != keyword]
        cats[new_cat].append(keyword)

    # new rules may also match other still-uncategorized rows
    st.session_state.df = categorize_data(df, cats, only_uncategorized=True)
    save_categories(cats)
    return len(changed)


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
def get_currency(df: pd.DataFrame) -> str:
    if "Currency" in df.columns and df["Currency"].notna().any():
        return str(df["Currency"].mode().iloc[0]).replace("%", "")
    return ""


def render_expenses_tab(debits: pd.DataFrame, currency: str):
    with st.expander("Add a new category"):
        with st.form("add_category_form", clear_on_submit=True):
            new_category = st.text_input("Category name").strip()
            if st.form_submit_button("Add Category") and new_category:
                if new_category in st.session_state.categories:
                    st.warning(f"Category '{new_category}' already exists.")
                else:
                    st.session_state.categories[new_category] = []
                    save_categories(st.session_state.categories)
                    st.session_state.flash = f"Category '{new_category}' added."
                    st.rerun()

    if debits.empty:
        st.info("No debit transactions found in this file.")
        return

    st.subheader("Transactions")
    st.caption("Change a category in the table, then click **Apply & Save Changes** "
               "so future uploads are categorised automatically.")

    editor_df = debits[["Date", "Details", "Amount", "Category"]]
    edited = st.data_editor(
        editor_df,
        column_config={
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Details": st.column_config.TextColumn("Details"),
            "Amount": st.column_config.NumberColumn("Amount", format=f"%.2f {currency}".strip()),
            "Category": st.column_config.SelectboxColumn(
                "Category", options=list(st.session_state.categories.keys()), required=True
            ),
        },
        disabled=["Date", "Details", "Amount"],
        hide_index=True,
        key=f"editor_{st.session_state.editor_version}",
    )

    if st.button("Apply & Save Changes", type="primary"):
        try:
            n = apply_edits(edited)
        except OSError as e:
            st.error(f"Could not save categories.json: {e}")
            return
        st.session_state.editor_version += 1  # reset the editor so edits aren't re-applied
        st.session_state.flash = f"Saved {n} change(s)." if n else "No changes to save."
        st.rerun()

    st.subheader("Expense Summary")
    summary = (
        debits.groupby("Category")["Amount"].sum().sort_values(ascending=False).reset_index()
    )
    summary["Share (%)"] = summary["Amount"] / summary["Amount"].sum() * 100
    st.dataframe(
        summary,
        column_config={
            "Amount": st.column_config.NumberColumn("Total Spent", format=f"%.2f {currency}".strip()),
            "Share (%)": st.column_config.NumberColumn("Share (%)", format="%.1f"),
        },
        hide_index=True,
    )

    fig = px.pie(summary, names="Category", values="Amount", hole=0.4,
                 title="Expenses by Category")
    fig.update_traces(
        textposition="inside",
        textinfo="percent+label",
        hovertemplate=f"<b>%{{label}}</b><br>%{{value:,.2f}} {currency}<br>%{{percent}}<extra></extra>",
    )
    st.plotly_chart(fig)


def render_credits_tab(credits: pd.DataFrame, currency: str):
    if credits.empty:
        st.info("No credit transactions found in this file.")
        return
    st.subheader("Payments & Income")
    st.dataframe(
        credits[["Date", "Details", "Amount"]],
        column_config={
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Amount": st.column_config.NumberColumn("Amount", format=f"%.2f {currency}".strip()),
        },
        hide_index=True,
    )


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Personal Finance Dashboard",
                       page_icon=":money_with_wings:", layout="wide")
    st.title("Personal Finance Dashboard")

    if "categories" not in st.session_state:
        st.session_state.categories = load_categories()
    st.session_state.setdefault("editor_version", 0)

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    uploaded = st.file_uploader(
        "Upload your bank statement (CSV, Excel or PDF)",
        type=["csv", "xls", "xlsx", "pdf"],
    )
    if uploaded is None:
        st.info("Upload a CSV, Excel or PDF statement with Date, Details, Amount and "
                "Debit/Credit columns to get started.")
        return

    file_id = f"{uploaded.name}-{uploaded.size}"
    if st.session_state.get("file_id") != file_id:
        try:
            df, warnings = clean_data(read_uploaded_file(uploaded))
        except Exception as e:
            st.session_state.pop("file_id", None)
            st.error(f"Error loading file: {e}")
            return
        st.session_state.df = categorize_data(df, st.session_state.categories)
        st.session_state.file_id = file_id
        st.session_state.warnings = warnings
        st.session_state.editor_version += 1

    for w in st.session_state.get("warnings", []):
        st.warning(w)

    df = st.session_state.df
    debits = df[df["Type"] == "Debit"]
    credits = df[df["Type"] == "Credit"]
    currency = get_currency(df)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Expenses", f"{debits['Amount'].sum():,.2f} {currency}".strip())
    c2.metric("Total Payments Received", f"{credits['Amount'].sum():,.2f} {currency}".strip())
    c3.metric("Uncategorized Items", int((debits["Category"] == "Uncategorized").sum()))

    tab1, tab2 = st.tabs(["Expenses (Debits)", "Payments & Income (Credits)"])
    with tab1:
        render_expenses_tab(debits, currency)
    with tab2:
        render_credits_tab(credits, currency)


if __name__ == "__main__":
    main()
