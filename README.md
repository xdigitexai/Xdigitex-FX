# Xdigitex Trade

Xdigitex Trade is an investor operations platform with real Xdigitex Pay deposit and mobile-money withdrawal API calls. It records provider references, reconciles payment status, and posts ledger entries only after provider confirmation. Broker trading and automated identity verification are not connected; there are no simulated trades, profits, or fabricated settlements.

## Xdigitex Pay integration

The backend follows the API contract supplied from `https://pay.xdigitex.space/docs`:

- Deposits use `POST /payments/initiate` with card, Safaricom, Airtel, Pan-Africa mobile money, or crypto options.
- Card and crypto checkout URLs are validated as HTTPS before returning them to the browser. The browser never receives the API key.
- Deposit credits are created only after `GET /payments/{reference}/status` reports `completed`, with matching reference, currency, gross amount, and reconciled fee/net amounts.
- The payment webhook at `/api/payments/webhook` treats the webhook event only as a prompt. Since the provided docs do not describe a webhook signature, the server fetches authenticated provider status before it can credit a deposit.
- Withdrawals use `POST /withdrawals` with `method: mobile_money` and an international phone number. Final status is reconciled from `GET /withdrawals`; only a matching completed provider record creates a ledger debit.
- Requests with an uncertain provider outcome are held for reconciliation and are not automatically retried or rejected, preventing accidental double charges or release of a possibly paid withdrawal.

The API key is a server secret. Never put it in frontend code or commit it. Copy `.env.example` to `.env`, put the key in the server environment, and set `PUBLIC_BASE_URL` to the public HTTPS URL of this product. For local development, callback delivery from Xdigitex Pay will not reach localhost; use a publicly reachable HTTPS deployment to test actual provider callbacks.

The documentation supplied specifies withdrawal minimums of KES 10, CDF 1,000, UGX 500, XOF/XAF/RWF 500, ZMW 5, and SLE 5. It gives conflicting withdrawal fee details (3% in prose and a 2.5% example). The app reserves up to the documented 3% while a request is pending, then records the actual fee returned by Xdigitex Pay.

## Run locally

```sh
cp .env.example .env
# Edit .env. Keep the API key private; leave it blank until Xdigitex Pay provides your merchant key.
docker compose up --build -d
```

Open `http://localhost:8080`. On the first run create an administrator:

```sh
docker compose exec web python server.py create-admin --email you@example.com --name "Xdigitex Operations"
```

Investor accounts register through the sign-in page. The admin manually records identity review outcomes after completing the real checks outside this product. Deposits and withdrawals are blocked until that review is marked verified.

For an actual provider integration, configure a valid merchant API key and an HTTPS `PUBLIC_BASE_URL` in the host environment before starting the application. Compose reads these values from `.env`. Set `COOKIE_SECURE=true` behind HTTPS (keep it false for local HTTP development). Never test with a real user payment until the merchant account, fees, and operating permissions are confirmed with Xdigitex Pay.

## Account and pool view

The dashboard uses an account-first layout with All / Real / Demo filters, a member reference, customer ledger balance, and provider status. The Real card represents a member ledger in Xdigitex’s managed pool; it is not a personal MT5 account number or a broker equity balance. Demo remains empty until an actual provider sandbox is integrated. Trade lots, closed trades, charts, equity, and order controls stay unavailable until the organization-owned pooled broker account supplies real data. The UI intentionally does not render sample candles or pretend an account has been provisioned.

## What is real in this build

- Persistent accounts, password hashing, CSRF protected state changes, sessions, administrator review, and a hash-linked audit record.
- Multi-currency customer balances and ledgers for KES, USD, CDF, UGX, XOF, XAF, RWF, ZMW, and SLE.
- Live provider API request code for Xdigitex Pay deposits and mobile-money withdrawals. The payment status is shown as configured only when the server key and public callback base URL are present; a real provider request is still needed to verify live connectivity.
- Deposit credits and withdrawal debits require an authenticated Xdigitex Pay status response and exact reference/amount/currency checks.

## What still needs real setup

- MT5 or another approved broker integration and the pooled account's real order/equity feeds. The trading controls remain disabled.
- An identity verification provider and approved customer due-diligence workflow. The present admin status is a manual operations record, not a KYC verification result from a vendor.
- Production deployment, TLS/reverse proxy, monitoring, backups, security review, reconciliation procedures, and authorized custody/financial arrangements.
- A valid Xdigitex Pay merchant account and API key. The code can be inspected without credentials, but real payment behavior cannot be verified until you configure your key and complete provider-approved transactions.

## API routes

- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me`
- `GET /api/dashboard`, `POST /api/requests/deposit`, `POST /api/requests/withdrawal`
- `GET /api/requests/{id}/status` — authenticated refresh against Xdigitex Pay
- `POST /api/payments/webhook` — provider callback; server confirms status with authenticated provider API
- `GET /api/admin/overview`, `POST /api/admin/requests/{deposit|withdrawal}/{id}/refresh`
- `POST /api/admin/users/{id}/kyc` — manual status record

State-changing user/admin routes require the `X-CSRF-Token` returned by `/api/me` or the authentication response. The Xdigitex Pay webhook is provider-facing and revalidates through the provider API.
