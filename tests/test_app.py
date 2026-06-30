import app
from app import add_merchant_rule, apply_category_overrides, apply_merchant_rules_to_transactions, categorize, infer_statement_profile, parse_transactions, summarize


def test_parse_transactions_from_common_statement_lines():
    text = """
    Statement Period 01/01/2026 - 01/31/2026
    01/03 Grocery Market -54.23
    01/05 PAYROLL DEPOSIT 2,450.00
    2026-01-10 Netflix Subscription ($15.99)
    """

    transactions = parse_transactions(text, "sample.pdf")

    assert len(transactions) == 3
    assert transactions[0].date == "2026-01-03"
    assert transactions[0].amount == -54.23
    assert transactions[0].category == "Groceries"
    assert transactions[1].amount == 2450.00
    assert transactions[1].category == "Income"
    assert transactions[2].amount == -15.99
    assert transactions[2].category == "Subscriptions"


def test_parse_transactions_uses_transaction_amount_not_running_balance():
    text = """
    Statement Period 01/01/2026 - 01/31/2026
    01/04 Coffee Shop 5.75 1,234.56
    01/05 Payroll Deposit 2,000.00 3,234.56
    Closing Balance 3,234.56
    """

    transactions = parse_transactions(text, "balance-column.pdf")

    assert len(transactions) == 2
    assert transactions[0].description == "Coffee Shop"
    assert transactions[0].amount == -5.75
    assert transactions[1].description == "Payroll Deposit"
    assert transactions[1].amount == 2000.0


def test_parse_amex_multiline_statement_transactions():
    text = """
    Blue Cash Everyday from American Express
    Account Ending 0-02003
    New Charges
    Detail
    Amount
    05/05/26 GOOGLE *FI R53HG5 G.CO/HELPPAY# CA
    TELECOM SERVICE
    $34.26
    Payments Amount
    05/28/26* AUTOPAY PAYMENT RECEIVED - THANK YOU
    JPMorgan Chase Bank, NA
    -$314.44
    """

    transactions = parse_transactions(text, "2026-06-03.pdf")

    assert transactions[0].account_name == "American Express Blue Cash Credit Card 2003"
    assert transactions[0].amount == -34.26
    assert transactions[1].amount == 314.44
    assert transactions[1].category == "Credit Card Payment"


def test_parse_chase_deposit_statement_sections():
    text = """
    JPMorgan Chase Bank, N.A.
    Checking & Savings
    A Monthly Service Fee was charged to your Chase College Checking account.
    04/28 American Express ACH Pmt A8076 Web ID: 9493560001 - 478.56 2,554.70
    05/15 Intellectt Inc Payroll PPD ID: 9111111103 6,207.46
    CHASE SAVINGS
    04/30 Online Transfer To Chk ...8912 Transaction#: 29025963799 - 1,000.00 6,650.73
    """

    transactions = parse_transactions(text, "Copy of May Statement.pdf")

    assert transactions[0].account_name == "Chase College Checking 8912"
    assert transactions[0].category == "Credit Card Payment"
    assert transactions[1].account_type == "checking"
    assert transactions[2].account_name == "Chase Savings 7225"


def test_yearless_dates_follow_statement_period_boundary():
    text = """
    JPMorgan Chase Bank, N.A.
    December 23, 2025 through January 26, 2026
    Checking & Savings
    CHASE COLLEGE CHECKING
    12/29 American Express ACH Pmt A9142 Web ID: 9493560001 - 454.88 4,109.23
    01/02 Chase Credit Crd Autopay PPD ID: 4760039224 - 274.94 1,467.23
    """

    transactions = parse_transactions(text, "Copy of January Statement.pdf")

    assert transactions[0].date == "2025-12-29"
    assert transactions[1].date == "2026-01-02"


def test_chase_card_december_rows_in_january_statement_use_prior_year():
    text = """
    Chase Credit Card
    Opening/Closing Date 12/05/25 - 01/04/26
    Statement Date: 01/04/26
    ACCOUNT ACTIVITY
    12/20 WAL-MART #5432 NEW RICHMOND WI 52.39
    01/02 PAYMENT THANK YOU -52.39
    """

    transactions = parse_transactions(text, "20260104-statements-3432-.pdf")

    assert transactions[0].date == "2025-12-20"
    assert transactions[1].date == "2026-01-02"


def test_parse_capital_one_360_month_name_bank_rows():
    text = """
    capitalone.com
    Here's your bank statement.April 2025 STATEMENT PERIOD
    Apr 4 - Apr 30, 2025
    360 Checking...2748 $0.00 $308.41
    DATE DESCRIPTION CATEGORY AMOUNT BALANCE
    Apr 4 Preauthorized Deposit from BANK OF AMERICA, N.A.
    checking account XXXXXXXX6329 Credit + $100.00 $100.00
    Apr 20 Digital Card Purchase - AMAZON MKTPL NR8NO34C3 AMZN
    COM BIL WA Debit - $408.19 $4,109.05
    """

    transactions = parse_transactions(text, "20250401-Bank statement.pdf")

    assert len(transactions) == 2
    assert transactions[0].account_name == "Capital One 360 Checking 2748"
    assert transactions[0].date == "2025-04-04"
    assert transactions[0].amount == 100.0
    assert transactions[1].description.endswith("COM BIL WA")
    assert transactions[1].amount == -408.19


def test_parse_compact_credit_card_tables():
    text = """
    QuicksilverOne Card | World Elite Mastercard ending in 8366 May 18, 2026 - Jun 16, 2026
    Transactions Visit capitalone.com to see detailed transactions.
    YASWANTH #8366: Payments, Credits and Adjustments Trans Date Post Date Description Amount
    May 26 May 26 REFERRAL BONUS - $100.00 Jun 8 Jun 8 CAPITAL ONE MOBILE PYMT - $130.00
    YASWANTH #8366: Transactions Trans Date Post Date Description Amount
    Jun 14 Jun 15 UBER *TRIP8005928996CA $22.95
    """

    transactions = parse_transactions(text, "Statement_062026_8366.pdf")

    assert [tx.amount for tx in transactions] == [100.0, 130.0, -22.95]
    assert transactions[0].account_name == "Capital One Credit Card 8366"


def test_parse_bofa_compact_credit_card_rows():
    text = """
    Customer Service Information:www.bankofamerica.com
    Visa Signature Account# 4400 6616 5654 1338 May 26 - June 25, 2026
    TransactionsTransactionDate PostingDate Description ReferenceNumber AccountNumber Amount
    Payments and Other Credits06/15 06/16 ONLINE/MOBILE PAYMENT CONF#fyefk44p6 9539 1338 -250.00
    Purchases and Adjustments05/26 05/27 CHIPOTLE MEX GR ONLINE TEAM-BANKING@CA 3171 1338 15.89
    05/27 05/27 LYFT *PRIORITY 05-25 LYFT.COM CA 4702 1338 16.97TOTAL PURCHASES
    """

    transactions = parse_transactions(text, "eStmt_2026-06-25.pdf")

    assert transactions[0].account_name == "Bank of America Credit Card 1338"
    assert transactions[0].amount == 250.0
    assert transactions[1].amount == -15.89
    assert transactions[2].description.startswith("LYFT")


def test_parse_multipart_body_without_cgi():
    boundary = "----boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n\r\n'
        "Yash\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="statement"; filename="sample.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    fields, files = app.parse_multipart_body({"Content-Type": f"multipart/form-data; boundary={boundary}"}, body)

    assert fields == {"name": ["Yash"]}
    assert files["statement"][0]["filename"] == "sample.pdf"
    assert files["statement"][0]["file"].read() == b"%PDF"


def test_categorize_falls_back_to_income_for_positive_amounts():
    assert categorize("Unknown ACH", 100.0) == "Income"
    assert categorize("Mystery Vendor", -20.0) == "Uncategorized"
    assert categorize("Online Payment Thank You", 300.0) == "Credit Card Payment"


def test_statement_profile_infers_account_type_and_institution():
    profile = infer_statement_profile("Minimum payment due Credit Limit", "chase-credit-card.pdf")

    assert profile["account_type"] == "credit_card"
    assert profile["account_name"].startswith("Chase")


def test_summarize_computes_cash_flow():
    transactions = [
        {"amount": 1000.0, "category": "Income", "date": "2026-01-01", "description": "Payroll", "statement": "a.pdf", "account_type": "checking"},
        {"amount": -250.0, "category": "Groceries", "date": "2026-01-02", "description": "Market", "statement": "a.pdf", "account_type": "credit_card"},
        {"amount": -250.0, "category": "Credit Card Payment", "date": "2026-01-10", "description": "Payment to card", "statement": "b.pdf", "account_type": "checking"},
    ]

    result = summarize(transactions, "Mia")

    assert result["income"] == 1000.0
    assert result["expenses"] == 250.0
    assert result["net"] == 750.0
    assert result["internal_out"] == 250.0
    assert result["by_month"]["2026-01"]["net"] == 750.0


def test_graphs_default_to_hidden_dashboard_panels():
    assert "graph-count-0" in app.app_shell()
    assert "savings-panel" in app.app_shell()
    assert "income-panel" in app.app_shell()
    assert "expenses-panel" in app.app_shell()
    assert "function defaultGraphSettings()" in app.JS
    assert "{ visible: false, type: chart.defaultType }" in app.JS
    assert "financialReviewGraphSettingsV3" in (app.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def test_template_can_load_static_assets_when_opened_directly():
    template = app.app_shell()

    assert '../static/styles.css' in template
    assert '../static/app.js' in template
    assert 'id="cache-status"' in template
    assert 'id="admin-workspace"' in template
    assert 'Admin Console' in template
    assert 'class="folder-strip"' not in template
    assert "cacheStatusText" in (app.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def test_user_password_is_set_on_first_login_and_required_after(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)

    user = app.authenticate_user("Password User", "first-pass")
    assert user["id"] == "password-user"
    assert user["has_password"] is True
    assert "password_hash" not in user

    same_user = app.authenticate_user("Password User", "first-pass")
    assert same_user["id"] == user["id"]

    try:
        app.authenticate_user("Password User", "wrong-pass")
    except PermissionError as error:
        assert str(error) == "PASSWORD_MISMATCH"
    else:
        raise AssertionError("Expected wrong password to be rejected")


def test_admin_can_manage_users(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)

    admin = app.authenticate_user("admin", "admin-pass")
    assert admin["is_admin"] is True

    created = app.admin_create_user("Managed User", "user-pass")
    assert created["id"] == "managed-user"
    assert any(user["id"] == "managed-user" for user in app.list_users())

    app.admin_change_password("managed-user", "new-pass")
    assert app.authenticate_user("Managed User", "new-pass")["id"] == "managed-user"

    try:
        app.authenticate_user("Managed User", "user-pass")
    except PermissionError as error:
        assert str(error) == "PASSWORD_MISMATCH"
    else:
        raise AssertionError("Expected old password to stop working")

    app.remove_user("managed-user")
    assert not app.user_dir("managed-user").exists()
    assert all(user["id"] != "managed-user" for user in app.list_users())


def test_merchant_rule_recategorizes_matching_transactions():
    transactions = [
        {"description": "DUNKIN #353523 JOHNS CREEK GA", "category": "Uncategorized"},
        {"description": "DUNKIN #123 ATLANTA GA", "category": "Dining"},
        {"description": "KROGER ALPHARETTA GA", "category": "Groceries"},
    ]

    updated = apply_merchant_rules_to_transactions([{"pattern": "DUNKIN", "category": "Dining"}], transactions)

    assert updated == 1
    assert transactions[0]["category"] == "Dining"
    assert transactions[1]["category"] == "Dining"
    assert transactions[2]["category"] == "Groceries"


def test_individual_override_wins_after_merchant_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user_id = "merchant-user"
    app.user_dir(user_id).mkdir(parents=True)
    transactions = [
        {
            "id": "tx-1",
            "statement": "a.pdf",
            "account_name": "Card",
            "date": "2026-01-01",
            "description": "DUNKIN #353523 JOHNS CREEK GA",
            "amount": -8.81,
            "category": "Uncategorized",
        }
    ]
    app.save_transactions(user_id, transactions)

    assert add_merchant_rule(user_id, "DUNKIN", "Dining")["updated_count"] == 1
    app.set_transaction_category(user_id, "tx-1", "Coffee")
    rescanned = app.load_transactions(user_id)
    apply_merchant_rules_to_transactions(app.load_merchant_rules(user_id), rescanned)
    apply_category_overrides(user_id, rescanned)

    assert rescanned[0]["category"] == "Coffee"


def test_scan_reuses_statement_cache_for_unchanged_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user = app.create_or_login_user("Cache User")
    statement = app.statements_dir(user["id"]) / "january.pdf"
    statement.write_bytes(b"fake pdf")
    calls = []

    def fake_extract(pdf_path):
        calls.append(pdf_path.name)
        return """
        Statement Period 01/01/2026 - 01/31/2026
        01/03 Grocery Market -54.23
        """

    monkeypatch.setattr(app, "extract_pdf_text", fake_extract)

    first_scan = app.scan_user_statements(user["id"])
    second_scan = app.scan_user_statements(user["id"])

    assert calls == ["january.pdf"]
    assert first_scan["parsed_statement_count"] == 1
    assert first_scan["cached_statement_count"] == 0
    assert second_scan["parsed_statement_count"] == 0
    assert second_scan["cached_statement_count"] == 1
    assert app.load_transactions(user["id"])[0]["description"] == "Grocery Market"


def test_scan_only_parses_new_statement_after_cache_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user = app.create_or_login_user("Incremental User")
    january = app.statements_dir(user["id"]) / "january.pdf"
    february = app.statements_dir(user["id"]) / "february.pdf"
    january.write_bytes(b"january")
    calls = []

    statement_text = {
        "january.pdf": "Statement Period 01/01/2026 - 01/31/2026\n01/03 Grocery Market -54.23",
        "february.pdf": "Statement Period 02/01/2026 - 02/28/2026\n02/03 Payroll Deposit 100.00",
    }

    def fake_extract(pdf_path):
        calls.append(pdf_path.name)
        return statement_text[pdf_path.name]

    monkeypatch.setattr(app, "extract_pdf_text", fake_extract)

    app.scan_user_statements(user["id"])
    february.write_bytes(b"february")
    second_scan = app.scan_user_statements(user["id"])

    assert calls == ["january.pdf", "february.pdf"]
    assert second_scan["parsed_statement_count"] == 1
    assert second_scan["cached_statement_count"] == 1
    assert second_scan["transaction_count"] == 2


def test_manual_transaction_is_saved_to_database_and_document(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user = app.create_or_login_user("Cash User")

    transaction = app.add_manual_transaction(
        user["id"],
        {
            "date": ["2026-02-14"],
            "account": ["Cash"],
            "description": ["Farmers Market"],
            "category": ["Groceries"],
            "amount": ["-18.50"],
        },
    )

    transactions = app.load_transactions(user["id"])
    document = app.read_json(app.manual_transactions_path(user["id"]), [])

    assert transaction is not None
    assert transactions[0]["statement"] == "Manual Entry"
    assert transactions[0]["account_type"] == "cash"
    assert transactions[0]["amount"] == -18.50
    assert document[0]["description"] == "Farmers Market"


def test_manual_transaction_can_be_removed_from_database_and_document(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user = app.create_or_login_user("Remove Cash User")
    transaction = app.add_manual_transaction(
        user["id"],
        {
            "date": ["2026-02-14"],
            "account": ["Cash"],
            "description": ["Cash Snack"],
            "category": ["Dining"],
            "amount": ["-6.25"],
        },
    )

    assert transaction is not None
    assert app.remove_manual_transaction(user["id"], transaction["id"])

    assert app.load_transactions(user["id"]) == []
    assert app.read_json(app.manual_transactions_path(user["id"]), []) == []


def test_savings_adjustment_sets_current_savings_total(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "USERS_DIR", tmp_path)
    user = app.create_or_login_user("Savings User")
    app.save_transactions(
        user["id"],
        [
            {
                "id": "income-1",
                "statement": "Manual Entry",
                "account_type": "cash",
                "account_name": "Cash",
                "date": "2026-01-01",
                "description": "Starting income",
                "amount": 100.0,
                "category": "Income",
                "source_line": "",
            }
        ],
    )

    transaction = app.add_savings_adjustment(
        user["id"],
        {"date": ["2026-01-02"], "account": ["Current holdings"], "current_amount": ["150.00"]},
    )
    result = summarize(app.load_transactions(user["id"]), "Mia")

    assert transaction is not None
    assert transaction["amount"] == 50.0
    assert transaction["category"] == "Savings Adjustment"
    assert result["savings"] == 150.0
