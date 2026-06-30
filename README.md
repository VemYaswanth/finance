# Financial Review

A local web app for reading PDF statements from each user's local folder, extracting transactions, categorizing spending, and showing a filterable financial summary.

## Run

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000.

Optional bind/port override:

```powershell
$env:HOST="0.0.0.0"
$env:PORT="8000"
python app.py
```

## Docker

For a Linux server that should run 24/7:

```bash
docker compose up -d --build
```

Open:

```text
http://<server-ip>:8000
```

The Compose file mounts `./data` into `/app/data`, so profiles, PDFs, raw text, manual transactions, and the SQLite database survive container rebuilds.

Useful commands:

```bash
docker compose logs -f
docker compose restart
docker compose down
```

To enable optional AI in Docker, uncomment the environment variables in `docker-compose.yml` or create shell environment variables before starting:

```bash
export OPENAI_API_KEY="your_api_key"
docker compose up -d
```

Enter a username on the main page. If the user does not exist, the app creates a local profile. If the username already exists, the app opens that user's existing database-backed workspace.

The app is folder-based. Put statement PDFs in `data/users/<user-id>/statements/`, then use **Refresh PDFs** in the app.

## Optional AI

The app works without an API key using local categorization rules and deterministic narrative summaries.

For richer AI summaries, set:

```powershell
$env:OPENAI_API_KEY="your_api_key"
python app.py
```

To also let AI improve categories during folder scans, set:

```powershell
$env:OPENAI_API_KEY="your_api_key"
$env:FIN_REVIEW_AI_CATEGORIZE="1"
python app.py
```

AI categorization only changes category labels. Amount extraction stays rule-based so the dollar values remain auditable.

Optional model override:

```powershell
$env:FIN_REVIEW_AI_MODEL="gpt-4.1-mini"
```

## Optional Plaid sync

The app can connect bank accounts with Plaid Link and import transactions through Plaid's Transactions Sync API. PDF statements still work without Plaid.

Set Plaid credentials before starting the app:

```powershell
$env:PLAID_CLIENT_ID="your_client_id"
$env:PLAID_SECRET="your_secret"
$env:PLAID_ENV="sandbox" # sandbox, development, or production
python app.py
```

Optional overrides:

```powershell
$env:PLAID_CLIENT_NAME="Financial Review"
$env:PLAID_PRODUCTS="transactions"
$env:PLAID_COUNTRY_CODES="US"
$env:PLAID_REDIRECT_URI="https://your-redirect-uri.example/callback"
```

After login, use **Connect Bank** to launch Plaid Link, then **Sync Plaid** for later incremental updates. Plaid access tokens and sync cursors are stored locally in `data/users/financial_review.sqlite3`.

If you import historical Plaid transactions for accounts that are also covered by PDFs, review totals for overlap. Plaid transactions are labeled with statement source `Plaid Sync`.

## Data layout

Each user gets a folder under `data/users/<user-id>/`:

- `profile.json`
- `manual_transactions.json`
- `statements/` for statement PDFs
- `raw_text/` for extracted PDF text used by the parser

The local SQLite database is stored at:

```text
data/users/financial_review.sqlite3
```

The database is the dashboard source of truth. It stores users, statements, transactions, category overrides, merchant rules, and statement fingerprints. Refresh scans only new or changed PDFs.

## Dates

January statements can include December transactions from the prior year. The parser uses statement closing dates, statement dates, date ranges, and filenames to resolve transactions where the PDF only shows `MM/DD`.

Example: a statement ending `01/04/26` with a transaction dated `12/20` is stored as `2025-12-20`, while `01/02` is stored as `2026-01-02`.

## Graphs and categories

The dashboard includes:

- Savings, income, and expenses trend graphs
- Category spending
- Account detail
- Top merchants

Click the username to open user details and rename a category globally. Use the category dropdown in each transaction row to recategorize a single transaction.

For recurring merchant cleanup, create a merchant rule. Example: set merchant text `DUNKIN` to category `Dining`, and every transaction whose description contains `DUNKIN` will move to `Dining`, even if it was previously Uncategorized or auto-categorized differently. Use the `Use Merchant` button in a transaction row to prefill the merchant rule form from that row.

Single-transaction edits are saved in `category_overrides.json`; merchant rules are saved in `merchant_rules.json`. Both are reapplied after future folder scans.

## Account types and overlap

The app tags transactions with an inferred account type:

- `checking`
- `savings`
- `credit_card`
- `unknown`

This matters because checking-account payments to credit cards can overlap with the purchases inside the credit card statement. The app treats `Credit Card Payment` and `Transfers` as internal flows. They remain visible in the transaction table, but they are excluded from expense totals and category spending charts to avoid double counting.

Example:

- Amex statement: `Grocery Store -75.00` counts as spending.
- Checking statement: `Payment to American Express -75.00` counts as an internal credit-card payment, not a second grocery expense.

## Notes

PDF statements vary a lot by bank. The parser handles common date-description-amount lines, skips statement totals/balances, and tries to avoid reading running-balance columns as transactions. It keeps raw extracted text so custom parsing rules can be added for specific statement formats.

For best accuracy, inspect `data/users/<user-id>/raw_text/` when a statement parses incorrectly. The raw text shows exactly what the PDF extractor saw, which makes it possible to add a bank-specific parsing rule.
