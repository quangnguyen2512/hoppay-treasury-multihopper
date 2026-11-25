# HopPay Treasury (`v1.0.0`) — Programmable Solana Payroll, Custom Routes & Agent Treasury Router

Built for the **MultiHopper Build & Route Challenge** on Superteam Earn using the official **MultiHopper v1 REST API** (`https://devnet.multihopper.com/api/v1` and `https://multihopper.com/api/v1`).

## What It Solves
Solana DAOs, creator agencies, launch teams, and autonomous AI agent treasuries need scheduled, multi-recipient SOL/USDC payouts with spending guardrails, custom routing control, cost transparency, audit receipts, and non-custodial signing. **HopPay Treasury** turns the complete MultiHopper v1 API into a hardened treasury operations console:

1. **Exact-Decimal CSV Import & Automatic 20-Wallet Chunking:** Upload or paste CSV payrolls (1–200 rows). Uses Python `Decimal` arithmetic (zero JS float drift), merges duplicate wallets, splits into uniform MultiHopper `destinations` routes (`<= 20` wallets/route — safe even for 10-hop custom routes across 21+ wallets), enforces `MH_017` SOL rent-exempt (`890,880` lamports) preflight checks, and supports idempotent `idempotency_key` retries + zero-orphan partial-chunk persistence.
2. **Custom Route Builder, Hop Library & Full Route Template CRUD:** Choose between **Auto Multi-Hop**, **Custom Routes** (`POST /api/v1/transfers` with explicit wallets or `GET /api/v1/hop-library` bundles `cex`/`dex`/`exchanges`/`kol`/`contracts`/`mix` + per-hop `waitSeconds`), or **Saved Route Templates** (`GET`/`POST`/`PATCH`/`DELETE /api/v1/route-templates`) with full `MH_018` / `MH_019` dry-run validation.
3. **Exact Fee, TRM Screening & Integrator Revenue Quoting (`POST /api/v1/transfers/estimate`):** Displays exact on-chain fee tier (Tiers 0–9), `sol.requiredSolUpFrontLamports` (including `dispersionRentLamports` and mainnet `0.002 SOL/wallet` TRM Labs compliance screening), and atomic on-chain **0.25% integrator revenue share**. Includes 1-click **`MH_040` / `MH_034` Re-Quote** (`POST /api/batches/{id}/transfers/{tid}/requote`) with superseded-transfer audit archiving.
4. **Enforced Treasury Guardrails, Approval Gate & Operator Auth:** Consolidates split-row duplicate wallets before checking per-recipient USD caps, locks high-value batches in `awaiting_approval`, blocks `/prepare` (`409 HOPPAY_APPROVAL_REQUIRED`) until human sign-off, and protects all mutating endpoints with constant-time `HOPPAY_ADMIN_TOKEN` authentication (`x-hoppay-admin-key` or `Authorization: Bearer`) for live public deployments.
5. **Non-Custodial Three-Way Signing (Browser Wallet + Node.js + Python):** Private keys never touch the backend.
   - **Browser UI:** Connect **Phantom / Solflare / Backpack** to sign and broadcast in strict protocol order while preserving signatures across `MH_041` / blockhash-expiry retries.
   - **Node.js CLI (`signer/broadcast_ordered.mjs`):** Automated ordered broadcaster with 3-second RPC propagation waits, `nothingToDo` resume recovery, `MH_040`/`MH_091` diagnostics, and `--poll` settlement tracking.
   - **Python Agent Signer (`signer/broadcast_ordered.py`):** `solders`-based VersionedTransaction (v0) signer preserving server ephemeral partial signatures with `--poll` parity for autonomous AI agent backends.
6. **HMAC-SHA256 Verified Webhooks, Explorer Links & Superteam Volume Proof Pack:** Verifies `x-multihopper-signature` with `hmac.compare_digest` across all 13 MultiHopper events idempotently, exports reconciliation receipts (`GET /api/batches/{id}/export.csv` and `.json`) with verifiable `business_purpose` + Solana Explorer links, and generates a live **Qualifying Volume Evidence Pack** (`GET /api/reports/qualifying-volume`) combining MultiHopper `GET /api/v1/usage` with settled batch proofs.

## Quickstart (Local Demo Mode — Zero API Keys Required)
```powershell
cd workspaces\superteam_da9333759fcb_multihopper

# Run full automated test suite (16 tests)
.\.venv\Scripts\python.exe -m pytest -v

# Start HopPay Treasury Control UI on http://127.0.0.1:8790
$env:PYTHONPATH = "src"
.\.venv\Scripts\uvicorn.exe hoppay.app:create_app --factory --host 127.0.0.1 --port 8790
```
In Demo mode (restart-safe via SQLite hydration), load any preset (DAO Payroll, Agency USDC, 25-Wallet Auto-Chunk, Approval Gate), click **1. Estimate MultiHopper Fees** -> **2. Quote & Build Routes**, approve if gated, and click **Simulate Settle** to watch the full two-stage broadcast + HMAC webhook settlement + receipt & volume-proof export execute live.

## Switching to Live Solana Devnet / Mainnet
1. Copy `.env.example` to `.env` and configure:
   - `HOPPAY_ADMIN_TOKEN=<strong-random-secret>` (required before exposing publicly!)
   - `MULTIHOPPER_ENV=devnet` + `MULTIHOPPER_API_KEY=mh_test_...` + `SOLANA_RPC_URL=https://api.devnet.solana.com` (for Devnet), OR
   - `MULTIHOPPER_ENV=mainnet` + `MULTIHOPPER_API_KEY=mh_live_...` + your Mainnet RPC URL (for Production).
2. Fund your registered MultiHopper rewards wallet above Solana rent-exempt minimum (`getMinimumBalanceForRentExemption(0)`) before routing mainnet transfers (prevents `MH_091`).
3. Register your HTTPS webhook (`https://<your-domain>/webhooks/multihopper`) via `POST /api/webhooks` (or `POST /api/v1/webhooks`) and set `MULTIHOPPER_WEBHOOK_SECRET=whsec_...`.
4. Sign and broadcast via **Phantom / Solflare** in the browser UI, or headlessly via:
   - Node.js: `cd signer && npm install && MULTIHOPPER_ENV=devnet MULTIHOPPER_API_KEY=mh_test_... SOLANA_KEYPAIR_PATH=~/.config/solana/devnet.json node broadcast_ordered.mjs <transferId> --poll`
   - Python: `pip install solders base58 httpx && MULTIHOPPER_ENV=devnet MULTIHOPPER_API_KEY=mh_test_... SOLANA_PRIVATE_KEY=<key> python signer/broadcast_ordered.py <transferId> --poll`

## 1-Click Cloud Deployment
Includes production `Dockerfile`, `render.yaml` (auto-generates `HOPPAY_ADMIN_TOKEN` and `MULTIHOPPER_WEBHOOK_SECRET`), and `railway.toml` with `/api/health` readiness checks.
