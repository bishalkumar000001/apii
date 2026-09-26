# VELOCITY BOTS API — Platform Upgrade

This build keeps the existing YouTube audio/video/search API and adds the first full SaaS layer.

## Branding
- VELOCITY BOTS API
- Telegram: https://t.me/Velocity_Bingo
- Admin/support: @Mr_Obstinate

## Included
- Public developer/pricing portal
- Account registration/login
- Session authentication
- Free/Basic/Pro/Premium plans
- INR wallet ledger
- Wallet transaction history API
- Subscription purchase from wallet
- User API key creation
- Per-subscription request quota
- API key authentication alongside the existing master API key
- Payment webhook endpoint with optional HMAC signature validation
- Admin overview/deposit APIs
- Existing search/audio/video/thumbnail/download endpoints retained
- 2160p quality ceiling by default

## Production Config Vars
Set these in Heroku rather than committing secrets:

- `API_KEY` — existing master API key
- `ADMIN_EMAIL` — email used for the admin account
- `PAYMENT_WEBHOOK_SECRET` — random secret shared with your payment gateway webhook
- `COOKIE_URL` — fresh authorized YouTube cookies URL
- `MAX_VIDEO_QUALITY=2160`

## Automatic UPI
A static UPI QR cannot reliably verify payment. The `/payments/webhook` endpoint is ready for a payment provider that sends signed successful-payment webhooks.

Webhook JSON expected:
```json
{
  "email": "user@example.com",
  "amount": 199,
  "status": "success",
  "reference": "gateway-payment-id"
}
```

If `PAYMENT_WEBHOOK_SECRET` is configured, send:
`X-Payment-Signature: HMAC_SHA256_HEX`

The server prevents duplicate crediting by payment reference.

## Important
The supplied payment layer does not claim that a bank QR has been automatically verified. Connect an approved UPI/payment gateway before enabling real-money automatic wallet crediting.
