# ARCHITECTURE — HopPay Treasury (`v1.0.0`)

Programmable Solana payroll, custom route studio, and AI-agent treasury console built natively on the **MultiHopper v1 REST API** (`https://devnet.multihopper.com/api/v1` and `https://multihopper.com/api/v1`).

## Core Invariants
1. **Strict Non-Custodial Boundary:** Private keys never touch the FastAPI backend or SQLite store. Signing happens exclusively client-side in Phantom/Solflare/Backpack (`src/hoppay/static/index.html`) or local headless signers (`signer/broadcast_ordered.mjs`, `signer/broadcast_ordered.py`).
2. **Protocol-Faithful Ordered Broadcast:** All three signers enforce MultiHopper's two-stage `confirm-broadcast` flow (`keeperFundingTx` -> immediate confirm -> `routeInitTxs` + 3s wait -> `orchestratorInitTx` legacy partialSign + 3s wait -> `sessionInitTxs` -> `dispersionInitTxs` -> final confirm) while preserving accumulated signatures across `MH_041` and blockhash-expiry retries.
3. **Treasury Guardrails & Security:** Consolidates split-row duplicate wallets before checking single-recipient caps, blocks `/prepare` until human approval (`409 HOPPAY_APPROVAL_REQUIRED`), protects mutating endpoints with constant-time `HOPPAY_ADMIN_TOKEN` auth when configured, and verifies all 13 MultiHopper webhooks with `hmac.compare_digest` (HMAC-SHA256).
4. **Zero-Orphan Ledger & Uniform Chunking:** Batches `>20` recipients auto-chunk into uniform MultiHopper `destinations` routes (`<=20`/route, preventing `MH_018` on 10-hop custom routes), persist partial chunk creations on upstream errors, and support `MH_040` tier-shift re-quoting with superseded-transfer audit trails.

## Directory Boundaries
```text
superteam_da9333759fcb_multihopper/
├── ARCHITECTURE.md            # System map & security invariants
├── README.md                  # Quickstart, live setup, deployment docs
├── SUBMISSION.md              # Superteam Earn submission copy & GTM checklist
├── pyproject.toml             # Python 3.10+ package (FastAPI, httpx, pydantic)
├── Dockerfile / render.yaml / railway.toml  # 1-click container deployment
├── src/hoppay/
│   ├── models.py              # Pydantic schemas, MH_017/018/019 guards, exact Decimal CSV math
│   ├── client.py              # MultiHopper v1 async client + restart-safe Demo simulator
│   ├── store.py               # SQLite WAL ledger, approval audit log, HMAC webhook store
│   ├── app.py                 # FastAPI REST API, operator auth, MH_040 requote, volume proofs
│   └── static/index.html      # Treasury Control UI + Phantom/Solflare browser broadcaster
├── signer/
│   ├── broadcast_ordered.mjs  # Headless Node.js (@solana/web3.js) ordered broadcaster
│   └── broadcast_ordered.py   # Headless Python (solders) VersionedTransaction (v0) signer
└── tests/test_hoppay.py       # 16 unit + contract + E2E security/lifecycle tests
```

## Execution DAG
```text
CSV / UI / Agent API -> models.py (policy + exact Decimal chunking) -> client.py (MultiHopper v1)
                                                                             |
SQLite WAL (store.py) <------------------------------------------------------+
       |
       +-> Human Approval Gate -> /prepare -> Client Signer (Browser/Node/Python) -> Solana RPC
       |                                                                                 |
       +<- HMAC Webhook (/webhooks/multihopper) & /reports/qualifying-volume <-----------+
```

## Active External Integrations
- **MultiHopper v1 REST API:** `devnet.multihopper.com/api/v1` (`mh_test_*`) and `multihopper.com/api/v1` (`mh_live_*`) — Transfers, Estimate, Hop Library, Route Templates CRUD, Webhooks, Usage.
- **Solana JSON-RPC:** Mainnet-Beta / Devnet RPC for client-side transaction confirmation & Solana Explorer settlement verification.
- **TRM Labs Compliance Gate:** Automatic on-chain screening enforced by MultiHopper mainnet smart contracts (`0.002 SOL/wallet` quoted via `/estimate`).

<!-- AUTO-KG-STATS:START -->
- **Knowledge Graph Live Sync:** `2026-11-25 16:08 UTC` | Nodes: **75** | Edges: **84** | Layers: **6** | Languages: `javascript, markdown, python`
- **Top Connected Hubs:** `src/hoppay/models.py` (19), `tests/test_hoppay.py` (17), `src/hoppay/app.py` (9), `signer/broadcast_ordered.py` (7), `src/hoppay/store.py` (5)
<!-- AUTO-KG-STATS:END -->
