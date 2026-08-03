# Broker authentication notes

## Current broker direction

The cockpit is focused on **Schwab + Hyperliquid**:

- Schwab handles stock/ETF account sync, previews, and guarded live order workflows.
- Hyperliquid handles read-only portfolio sync and local signed-submit workflow through API wallet credentials.
- The app remains paper/planning-first by default, with live actions controlled by explicit local `.env` settings.

## Credential handling rules

- Never commit real API keys, private keys, account IDs, passwords, or MFA backup codes.
- Use `.env` for local development secrets.
- Keep `.env` ignored by git.
- Prefer read-only credentials first when adding a new connector.
- Use separate API wallets/credentials when a platform supports it.
- Keep max-order-size controls in `.env` for live workflows.

## Schwab setup checklist

1. Create or verify Schwab Trader API app credentials.
2. Set `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, and `SCHWAB_REDIRECT_URI` in local `.env`.
3. Keep `SCHWAB_ENABLE_LIVE_ORDERS=false` until live order flow is intentionally enabled.
4. Use account refresh / preview flows before live submit.
5. Keep `SCHWAB_MAX_LIVE_ORDER_DOLLARS` set to a reasonable local cap.

## Hyperliquid setup checklist

1. Create or authorize a Hyperliquid API wallet.
2. Set the main/sub-account wallet as `HYPE_WALLET_ADDRESS`.
3. Set the API wallet address as `HYPE_API_ADDRESS`.
4. Set the API wallet private key as `HYPE_API_SECRET` in local `.env` only.
5. Keep `HYPERLIQUID_ENABLE_LIVE_ORDERS=false` until the local signed submit hook is intentionally wired and tested.
6. Keep `HYPERLIQUID_MAX_LIVE_ORDER_DOLLARS` set to a reasonable local cap.

## Live-action expectations

Before adding or changing any live broker adapter, the app should preserve:

- Paper/planning mode as the default mental model.
- Local `.env` opt-in for live submit.
- Max order size checks.
- Position/risk visibility before submit.
- Clear post-action account refresh.
- No committed secrets.
