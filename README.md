# Xdigitex Trade

Xdigitex Trade is an investor operations platform with real Xdigitex Pay deposit and mobile-money withdrawal API calls. It records provider references, reconciles payment status, and posts ledger entries only after provider confirmation. An Xdigitex internal trade engine, member-unit accounting, pool NAV, settlement rules, and automated identity verification are not implemented. There are no simulated trades, profits, or fabricated settlements.

## Xdigitex Pay integration

The backend follows the API contract supplied from `https://pay.xdigitex.space/docs`:

- Deposits use `POST /payments/initiate` with card, Safaricom, Airtel, Pan-Africa mobile money, or crypto options.
- Card and crypto checkout URLs are validated as HTTPS before returning them to the browser. The browser never receives the API key.
- Deposit credits are created only after `GET /payments/{reference}/status` reports `completed`, with matching reference, currency, gross amount, and reconciled fee/net amounts.
- The payment webhook at `/api/payments/webhook` treats the webhook event only as a prompt. Since the provided docs do not describe a webhook signature, the server fetches authenticated provider status before it can credit a deposit.
- Withdrawals use `POST /withdrawals` with `method: mobile_money` and an international phone number. Final status is reconciled from `GET /withdrawals`; only a matching completed provider record creates a ledger debit.
- Requests with an uncertain provider outcome are held for reconciliation and are not automatically retried or rejected, preventing accidental double charges or release of a possibly paid withdrawal.

The Xdigitex Pay key is a server secret. Never put it in frontend code or commit it. Copy `.env.example` to `.env`, put the key in the server environment, and set `PUBLIC_BASE_URL` to the public HTTPS URL of this product. The optional `TWELVE_DATA_API_KEY` is a separate read-only market-data credential; it does not enable trading. For local development, callback delivery from Xdigitex Pay will not reach localhost; use a publicly reachable HTTPS deployment to test actual provider callbacks.

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

## Internal pool and market data

The account page shows a member's payment-confirmed ledger balance. It is not trading equity, pool NAV, or an allocated number of pool units. There is no broker dependency and no external order route. The optional Twelve Data integration provides cached, read-only FX reference prices for EUR/USD, GBP/USD, USD/JPY, and USD/CHF at `/api/market/quotes`, together with the recent 1-minute closes behind each price. Set `TWELVE_DATA_API_KEY` on the server to show prices; `MARKET_DATA_CACHE_SECONDS` defaults to 600 and is bounded from 60 to 3,600 seconds.

The landing page and the trade terminal both draw a reference price line chart from that endpoint. The chart plots provider 1-minute closes only, is labelled "not executable", and shows an explicit "no live data" state when the feed is unconfigured or unavailable. No candle, volume, or trade series is fabricated, and nothing is synthesised in the browser: the rolling window is held and refreshed server-side.

Quotes do not execute orders or confirm any trade. The internal order engine, price timestamp policy, position/risk ledger, member-unit accounting, pool NAV, and settlement/reconciliation rules still need implementation. Order controls remain unavailable, and the UI does not show sample candles or estimated returns. Demo is empty and contains no fake account.

## What is real in this build

- Persistent accounts, password hashing, CSRF protected state changes, sessions, administrator review, and a hash-linked audit record.
- Multi-currency customer balances and ledgers for KES, USD, CDF, UGX, XOF, XAF, RWF, ZMW, and SLE.
- Live provider API request code for Xdigitex Pay deposits and mobile-money withdrawals. The payment status is shown as configured only when the server key and public callback base URL are present; a real provider request is still needed to verify live connectivity.
- Optional read-only FX price requests through Twelve Data, centrally cached for all users. The data key remains server-side, the trend window is kept on the server, and the prices are never represented as order execution.
- Deposit credits and withdrawal debits require an authenticated Xdigitex Pay status response and exact reference/amount/currency checks.

## What still needs real setup

- The Xdigitex-owned order engine, position and risk ledger, member-unit allocation, pool NAV calculation, settlement policy, and audit/reconciliation flow. Trading stays disabled until those parts are implemented and reviewed.
- An identity verification provider and approved customer due-diligence workflow. The present admin status is a manual operations record, not a KYC verification result from a vendor.
- Production deployment, TLS/reverse proxy, monitoring, backups, security review, reconciliation procedures, and authorized custody/financial arrangements.
- A valid Xdigitex Pay merchant account and API key. The code can be inspected without credentials, but real payment behavior cannot be verified until you configure your key and complete provider-approved transactions.

## API routes

- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me`
- `GET /api/dashboard`, `POST /api/requests/deposit`, `POST /api/requests/withdrawal`
- `GET /api/market/quotes` — optional cached read-only FX prices plus their recent 1-minute closes; never executes trades
- `GET /api/market/history` — optional cached read-only FX trend series for the reference charts
- `GET /api/requests/{id}/status` — authenticated refresh against Xdigitex Pay
- `POST /api/payments/webhook` — provider callback; server confirms status with authenticated provider API
- `GET /api/admin/overview`, `POST /api/admin/requests/{deposit|withdrawal}/{id}/refresh`
- `POST /api/admin/users/{id}/kyc` — manual status record

State-changing user/admin routes require the `X-CSRF-Token` returned by `/api/me` or the authentication response. The Xdigitex Pay webhook is provider-facing and revalidates through the provider API.
