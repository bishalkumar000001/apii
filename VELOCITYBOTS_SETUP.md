# VelocityBots Paid API — deployment guide

## Included
- Existing FastAPI routes remain available: `/health`, `/search`, `/thumbnail`, `/download`, `/video`, and `/files/{filename}`.
- Customer portal: `/account`; owner panel: `/admin`; OpenAPI docs: `/docs`.
- Email/password accounts and optional Google Identity sign-in.
- Personal API keys are generated with a cryptographically secure random value. Only SHA-256 hashes are stored; the full key is shown once.
- Monthly subscriptions and wallet credits in INR, manual payment requests, owner approval/rejection, usage logs, and account suspension.

## 1. MongoDB Atlas
Create a MongoDB Atlas cluster and database user. Restrict network access to your deployment where practical. Copy the connection URI into `MONGODB_URI` and replace the URI's username/password placeholders securely. Never commit credentials.

## 2. Required deployment config vars
- `MONGODB_URI`: Atlas connection string (required).
- `MONGODB_DATABASE`: `velocitybots_api` (or your preferred DB name).
- `SESSION_SECRET`: random secret, at least 32 characters. Generate one locally, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `COOKIE_SECURE`: `true` on HTTPS hosting (recommended).
- `ADMIN_EMAIL`: email for the owner account.
- `ADMIN_BOOTSTRAP_TOKEN`: another random one-time secret.
- `GOOGLE_CLIENT_ID`: optional; configure a Google OAuth Web application client ID and authorized JavaScript origins for your deployed HTTPS domain to enable Google sign-in.
- `LEGACY_API_KEY_ENABLED`: leave `false` to enforce per-customer billing.

Keep existing variables such as `COOKIE_URL`, `DOWNLOAD_DIR`, `CACHE_EXPIRE_HOURS`, `MAX_VIDEO_QUALITY`, and retry/download settings if your current deployment uses them.

## 3. First owner setup
1. Set `ADMIN_EMAIL` to the owner email and `ADMIN_BOOTSTRAP_TOKEN` to a separate random secret (at least 20 characters).
2. Deploy the updated project, then open `/docs` on your own API host.
3. Use `POST /api/admin/bootstrap-account` with this JSON body (send it over HTTPS only):

```json
{
  "email": "the-same-address-as-ADMIN_EMAIL",
  "password": "a-unique-password-of-at-least-10-characters",
  "token": "your-configured-one-time-bootstrap-token"
}
```

4. The endpoint creates the first owner account and signs you in. Remove `ADMIN_BOOTSTRAP_TOKEN` from hosting config immediately and restart/redeploy. Owner panel: `/admin`.
5. The owner email is blocked from public registration, so an unauthenticated visitor cannot pre-claim the owner address.

## 4. Payments and plans
The built-in example plans are Starter ₹199/30 days/1,000 requests, Pro ₹499/30 days/5,000 requests, and Business ₹999/30 days/15,000 requests. Change the `PLANS` list in `billing.py` before production if you want different pricing or limits. Customers submit the payment method and transaction reference; the owner must independently verify the payment and approve it in `/admin`. Approval credits the wallet or activates the selected plan. Never approve based only on a screenshot or an unverified claim.

Wallet usage is ₹0.01 per authenticated API request (one paise); subscriptions use one quota unit per authenticated API request. Subscription quota is consumed before wallet credits. Failed downstream API operations can still consume a unit because metering occurs during API-key authorization.

## 5. API key migration
The previous single shared key is disabled by default so it cannot bypass the new billing system. Customers should generate their own key in `/account` and use it as `X-API-Key: vb_...` or `Authorization: Bearer vb_...`. The `?api_key=` form is still supported for older clients, but headers are safer because URLs may be logged. Do not enable `LEGACY_API_KEY_ENABLED=true` in production unless you intentionally accept that the shared key bypasses billing.

## 6. Google sign-in
Create a Google OAuth Client ID of type Web application. Add the production site origin to Authorized JavaScript origins and set `GOOGLE_CLIENT_ID`. The backend verifies the Google ID token against that audience. Email/password login remains available without Google configuration.

## Important production notes
- Configure MongoDB backups and restricted network access.
- Set a strong unique `SESSION_SECRET`; changing it invalidates existing signed sessions.
- Add your production domain and support/privacy/terms pages before publicly selling access.
- Protect the `/admin` owner account with a strong password and Google account security.
- Current wallet top-ups and subscription payments are manual, not automatically confirmed by a payment provider.

## Website upgrades and manual payment display

The developer portal now includes pricing cards, FAQ, links to the customer dashboard, and the existing live endpoint tester. The customer dashboard includes recent API activity. The owner panel includes request activity analytics in addition to payment approvals and account controls.

To display your own manual payment details on `/account`, set these Heroku Config Vars:

- `PAYMENT_UPI_ID` — your UPI ID (leave blank if you do not accept UPI).
- `PAYMENT_ACCOUNT_NAME` — the recipient name customers should verify.
- `PAYMENT_QR_URL` — optional HTTPS URL to your payment QR image. Use a public image URL you control.
- `PAYMENT_INSTRUCTIONS` — optional instructions shown to customers.

These settings only display payment instructions; they do not verify payments automatically. Always confirm funds arrived in your account before approving a request from `/admin`. Do not put bank passwords, UPI PINs, OTPs, or private credentials in website configuration.
