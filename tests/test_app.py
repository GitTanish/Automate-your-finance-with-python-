"""Simple tests for the upload, parsing and categorisation logic in app.py.

Run with:  pytest
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pandas as pd
import pytest

import app

SAMPLE_CSV = Path(__file__).parent.parent / "sample_bank_statement.csv"


class FakeUpload(io.BytesIO):
    """Mimics Streamlit's UploadedFile, which exposes .size and .name."""

    def __init__(self, data: bytes, name: str = "statement.csv"):
        super().__init__(data)
        self.size = len(data)
        self.name = name


def upload(text: str, encoding: str = "utf-8", name: str = "statement.csv") -> FakeUpload:
    return FakeUpload(text.encode(encoding), name=name)


@pytest.fixture
def tmp_categories(tmp_path, monkeypatch):
    """Point categories.json at a temporary file so the real one is never touched."""
    path = tmp_path / "categories.json"
    monkeypatch.setattr(app, "CATEGORY_FILE", path)
    return path


# ----------------------------------------------------------------------------
# read_csv_safely
# ----------------------------------------------------------------------------
def test_read_csv_rejects_empty_file():
    with pytest.raises(ValueError, match="empty"):
        app.read_csv_safely(FakeUpload(b""))


def test_read_csv_reads_utf8_with_bom():
    df = app.read_csv_safely(upload("\ufeffDate,Amount\n01-Jan-25,10\n"))
    assert list(df.columns) == ["Date", "Amount"]


def test_read_csv_falls_back_to_latin1():
    # 0xE9 is "é" in latin-1 and is not valid UTF-8 on its own
    raw = "Date,Details,Amount\n01-Jan-25,CAFÉ,10\n".encode("latin-1")
    df = app.read_csv_safely(FakeUpload(raw))
    assert df.loc[0, "Details"] == "CAFÉ"


def test_read_csv_header_only_raises_no_rows_error():
    # pandas returns an empty frame here rather than raising, so clean_data reports it
    df = app.read_csv_safely(upload("Date,Details,Amount\n"))
    with pytest.raises(ValueError, match="No valid transactions"):
        app.clean_data(df)


# ----------------------------------------------------------------------------
# Excel and PDF readers
# ----------------------------------------------------------------------------
def make_xlsx() -> bytes:
    buf = io.BytesIO()
    pd.DataFrame({
        "Date": ["01-Jan-25", "02-Jan-25"],
        "Details": ["NOON.COM", "SALARY"],
        "Amount": [1000, 5000],
        "Debit/Credit": ["DR", "CR"],
    }).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def make_pdf(*lines: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line)
        y += 16
    data = doc.tobytes()
    doc.close()
    return data


def test_read_uploaded_file_dispatches_csv_by_default():
    df = app.read_uploaded_file(upload("Date,Amount\n01-Jan-25,10\n"))
    assert list(df.columns) == ["Date", "Amount"]


def test_read_xlsx_statement():
    raw = app.read_uploaded_file(FakeUpload(make_xlsx(), name="statement.xlsx"))
    df, warnings = app.clean_data(raw)
    assert warnings == []
    assert df["Details"].tolist() == ["NOON.COM", "SALARY"]
    assert df["Type"].tolist() == ["Debit", "Credit"]
    assert df["Amount"].tolist() == [1000.0, 5000.0]


def test_read_pdf_statement():
    pdf = make_pdf(
        "28-Feb-25 Card Payment Received 18,551.62 CR",
        "15-Feb-25 EMIRATES INSURANCE 137.95 DR",
        "05-Oct-24 LULU HYPERMARKET 455.0 DR",
    )
    raw = app.read_uploaded_file(FakeUpload(pdf, name="statement.pdf"))
    df, _ = app.clean_data(raw)
    assert df["Details"].tolist() == ["Card Payment Received", "EMIRATES INSURANCE", "LULU HYPERMARKET"]
    assert df["Type"].tolist() == ["Credit", "Debit", "Debit"]
    assert df["Amount"].tolist() == [18551.62, 137.95, 455.0]


@pytest.mark.parametrize("name", ["statement.xlsx", "statement.pdf"])
def test_new_readers_reject_empty_file(name):
    with pytest.raises(ValueError, match="empty"):
        app.read_uploaded_file(FakeUpload(b"", name=name))


def test_read_pdf_rejects_non_pdf():
    with pytest.raises(ValueError, match="Could not read the PDF"):
        app.read_uploaded_file(FakeUpload(b"not a pdf", name="statement.pdf"))


# ----------------------------------------------------------------------------
# Value helpers
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("1,234.50", 1234.50),
    ("AED 99", 99.0),
    ("$12", 12.0),
    ("-45.10", -45.10),
    ("(45.10)", -45.10),
    ("18,551.62", 18551.62),
])
def test_to_number(raw, expected):
    result = app._to_number(pd.Series([raw]))
    assert result.iloc[0] == pytest.approx(expected)


def test_to_number_unparseable_becomes_nan():
    assert pd.isna(app._to_number(pd.Series(["n/a"])).iloc[0])


@pytest.mark.parametrize("raw, expected", [
    ("28-Feb-25", "2025-02-28"),
    ("28-Feb-2025", "2025-02-28"),
    ("28/02/2025", "2025-02-28"),
    ("2025-02-28", "2025-02-28"),
    ("28 Feb 2025", "2025-02-28"),
])
def test_parse_dates_known_formats(raw, expected):
    result = app._parse_dates(pd.Series([raw]))
    assert result.iloc[0] == pd.Timestamp(expected)


def test_parse_dates_unparseable_becomes_nat():
    assert pd.isna(app._parse_dates(pd.Series(["not a date"])).iloc[0])


@pytest.mark.parametrize("raw, expected", [
    ("Debit", "Debit"),
    ("DR", "Debit"),
    ("withdrawal", "Debit"),
    ("Credit", "Credit"),
    ("cr", "Credit"),
    ("deposit", "Credit"),
    ("unknown", None),
])
def test_normalise_type(raw, expected):
    assert app._normalise_type(raw) == expected


# ----------------------------------------------------------------------------
# clean_data
# ----------------------------------------------------------------------------
def test_clean_data_sample_statement():
    raw = app.read_csv_safely(FakeUpload(SAMPLE_CSV.read_bytes()))
    df, warnings = app.clean_data(raw)

    assert warnings == []
    assert list(df.columns) == ["Date", "Details", "Amount", "Currency", "Type"]
    assert len(df) == len(raw)
    assert (df["Amount"] >= 0).all()
    assert df["Date"].notna().all()

    # the sample labels every row with Debit/Credit, so the counts must match the file
    assert (df["Type"] == "Credit").sum() == (raw["Debit/Credit"] == "Credit").sum()

    first = df.iloc[0]
    assert first["Date"] == pd.Timestamp("2025-02-28")
    assert first["Amount"] == pytest.approx(18551.62)
    assert first["Type"] == "Credit"


def test_clean_data_recognises_alias_headers():
    csv = (
        "Transaction Date,Narration,Transaction Amount,Dr/Cr\n"
        "01-Jan-25,NOON.COM,\"1,000.00\",DR\n"
        "02-Jan-25,SALARY,5000,CR\n"
    )
    df, warnings = app.clean_data(app.read_csv_safely(upload(csv)))
    assert warnings == []
    assert df["Details"].tolist() == ["NOON.COM", "SALARY"]
    assert df["Type"].tolist() == ["Debit", "Credit"]
    assert df["Amount"].tolist() == [1000.0, 5000.0]


def test_clean_data_missing_required_column_names_it():
    csv = "Date,Amount\n01-Jan-25,10\n"
    with pytest.raises(ValueError, match="Details/Description"):
        app.clean_data(app.read_csv_safely(upload(csv)))


def test_clean_data_without_type_column_infers_from_sign_and_warns():
    csv = "Date,Details,Amount\n01-Jan-25,COFFEE,-12.50\n02-Jan-25,REFUND,20\n"
    df, warnings = app.clean_data(app.read_csv_safely(upload(csv)))
    assert df["Type"].tolist() == ["Debit", "Credit"]
    assert df["Amount"].tolist() == [12.5, 20.0]
    assert any("No Debit/Credit column" in w for w in warnings)


def test_clean_data_skips_bad_rows_and_warns():
    csv = (
        "Date,Details,Amount,Debit/Credit\n"
        "01-Jan-25,GOOD,10,Debit\n"
        "garbage,BAD DATE,10,Debit\n"
        "02-Jan-25,BAD AMOUNT,xyz,Debit\n"
    )
    df, warnings = app.clean_data(app.read_csv_safely(upload(csv)))
    assert df["Details"].tolist() == ["GOOD"]
    assert any("Skipped 2 row(s)" in w for w in warnings)


def test_clean_data_all_rows_bad_raises():
    csv = "Date,Details,Amount,Debit/Credit\ngarbage,X,xyz,Debit\n"
    with pytest.raises(ValueError, match="No valid transactions"):
        app.clean_data(app.read_csv_safely(upload(csv)))


def test_clean_data_keeps_currency_when_present():
    csv = "Date,Details,Amount,Currency\n01-Jan-25,NOON,10,AED\n"
    df, _ = app.clean_data(app.read_csv_safely(upload(csv)))
    assert df["Currency"].tolist() == ["AED"]


# ----------------------------------------------------------------------------
# Categorisation
# ----------------------------------------------------------------------------
CATS = {
    "Shopping": ["noon", "amazon"],
    "Groceries": ["lulu", "lulu hypermarket"],
    "Subscriptions": ["netflix"],
    "Uncategorized": [],
}


@pytest.mark.parametrize("desc, expected", [
    ("NOON.COM", "Shopping"),
    ("netflix.com", "Subscriptions"),
    ("AMAZON AE", "Shopping"),
    ("SOMETHING ELSE", "Uncategorized"),
    ("", "Uncategorized"),
])
def test_categorize_transaction_basic(desc, expected):
    assert app.categorize_transaction(desc, CATS) == expected


def test_categorize_transaction_longest_keyword_wins():
    assert app.categorize_transaction("LULU HYPERMARKET DUBAI", CATS) == "Groceries"


def test_categorize_data_fills_category_column():
    df = pd.DataFrame({"Details": ["NETFLIX.COM", "MYSTERY"], "Amount": [1, 2]})
    out = app.categorize_data(df, CATS)
    assert out["Category"].tolist() == ["Subscriptions", "Uncategorized"]
    assert "Category" not in df.columns  # input is not mutated


def test_categorize_data_only_uncategorized_keeps_existing_labels():
    df = pd.DataFrame({
        "Details": ["NETFLIX.COM", "NOON.COM"],
        "Amount": [1, 2],
        "Category": ["Travel", "Uncategorized"],
    })
    out = app.categorize_data(df, CATS, only_uncategorized=True)
    assert out["Category"].tolist() == ["Travel", "Shopping"]


# ----------------------------------------------------------------------------
# Category storage
# ----------------------------------------------------------------------------
def test_load_categories_creates_defaults_when_missing(tmp_categories):
    assert not tmp_categories.exists()
    cats = app.load_categories()
    assert cats == app.DEFAULT_CATEGORIES
    assert tmp_categories.exists()


def test_load_categories_backs_up_corrupted_file(tmp_categories):
    tmp_categories.write_text("{not json", encoding="utf-8")
    cats = app.load_categories()
    assert cats == app.DEFAULT_CATEGORIES
    assert tmp_categories.with_suffix(".json.bak").read_text(encoding="utf-8") == "{not json"


def test_load_categories_round_trip(tmp_categories):
    app.save_categories({"Food": ["pizza"]})
    assert app.load_categories() == {"Food": ["pizza"], "Uncategorized": []}


# ----------------------------------------------------------------------------
# apply_edits (learns new keyword rules)
# ----------------------------------------------------------------------------
def test_apply_edits_updates_category_and_learns_rule(tmp_categories, monkeypatch):
    categories = {"Shopping": ["noon"], "Food": [], "Uncategorized": []}
    df = pd.DataFrame({
        "Details": ["pizza place", "noon.com"],
        "Amount": [30.0, 10.0],
        "Category": ["Uncategorized", "Shopping"],
    })
    fake_st = SimpleNamespace(session_state=SimpleNamespace(df=df, categories=categories))
    monkeypatch.setattr(app, "st", fake_st)

    edited = df.copy()
    edited.loc[0, "Category"] = "Food"

    changed = app.apply_edits(edited)

    assert changed == 1
    assert fake_st.session_state.df.loc[0, "Category"] == "Food"
    assert "pizza place" in categories["Food"]
    saved = json.loads(tmp_categories.read_text(encoding="utf-8"))
    assert "pizza place" in saved["Food"]
