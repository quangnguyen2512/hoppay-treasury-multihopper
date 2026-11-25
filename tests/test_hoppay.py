"""Unit & integration tests for HopPay Treasury (MultiHopper Build & Route Challenge)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hoppay.app import create_app
from hoppay.client import MultiHopperAPIError, MultiHopperClient, compute_official_estimate
from hoppay.models import (
    SOL_MINT,
    BatchPayoutRequest,
    CustomHop,
    CustomRoute,
    PayoutRecipient,
    chunk_recipients_for_multihopper,
    evaluate_treasury_policy,
    parse_payout_csv,
    validate_custom_route,
)
from hoppay.store import HopPayStore, aggregate_batch_status, sign_webhook_payload

WALLET_A = "7pBqizvE8vQZ29F49NWSu9cho3s7M9SqJyNN1UoQyQ2u"
WALLET_B = "3UdgSTpcUbiBSmgdPBiVHRMbcDS731kMT6bAQQZgE3c4"
WALLET_C = "64CuV85HHCydAwxQMoXwZ9HXoyw5qE65NkiJxCtuFErF"
WALLET_D = "DQTf1yf1YM4B48PqJyQ53SEWGDM6ib4YBV7P3wm2MCxj"


def test_official_pricing_schedule_matches_multihopper_worked_example():
    """Matches exact worked example in dev-docs.multihopper.com/concepts/pricing (1 SOL @ $120, 3 hops)."""
    est = compute_official_estimate(
        token_mint=SOL_MINT,
        amount_raw=1_000_000_000,
        token_decimals=9,
        hops=3,
        token_price_usd=120.0,
    )
    assert est["tier"] == "tier_2"
    assert est["totalFlatFeeLamports"] == 1_125_000
    assert est["percentFeeBps"] == 50
    assert int(est["percentFeeRaw"]) == 4_969_527
    assert int(est["hopAmountRaw"]) == 993_905_472
    assert int(est["integratorPercentShare"]) == 2_484_763


def test_csv_parse_and_auto_chunking_25_recipients_into_two_routes():
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    wallets = [f"7pBqizvE8vQZ29F49NWSu9cho3s7M9SqJyNN1UoQ{alphabet[i]}{alphabet[i+1]}" for i in range(25)]
    csv_lines = ["wallet,amount,label"] + [f"{w},0.5,Dev {idx}" for idx, w in enumerate(wallets)]
    csv_lines.append(f"{wallets[0]},0.25,Dev 0 Bonus")
    recipients = parse_payout_csv("\n".join(csv_lines))
    assert len(recipients) == 26

    chunks = chunk_recipients_for_multihopper(recipients, decimals=9, is_sol=True)
    assert len(chunks) == 2
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 5
    assert chunks[0][0]["amountRaw"] == "750000000"


def test_mh_017_preflight_blocks_dust_sol_destination():
    recs = [PayoutRecipient(wallet=WALLET_B, amount_tokens="0.0005", label="Too small")]
    with pytest.raises(ValueError, match="MH_017"):
        chunk_recipients_for_multihopper(recs, decimals=9, is_sol=True)


def test_treasury_policy_caps_approval_and_split_row_bypass_prevention():
    req = BatchPayoutRequest(
        batch_name="High Value Payout",
        source_owner=WALLET_A,
        token_price_usd=150.0,
        recipients=[
            PayoutRecipient(wallet=WALLET_B, amount_tokens="20", label="Senior Eng"),  # $3,000
        ],
    )
    decision = evaluate_treasury_policy(req, max_single_recipient_usd=10_000.0, approval_threshold_usd=2_500.0)
    assert decision.allowed is True
    assert decision.requires_human_approval is True
    assert decision.total_usd == 3000.0

    over_cap = evaluate_treasury_policy(req, max_single_recipient_usd=2_000.0, approval_threshold_usd=1_000.0)
    assert over_cap.allowed is False
    assert over_cap.violations

    # Split-row bypass attempt: two $6,000 rows to the SAME wallet must violate $10,000 single-recipient cap
    split_req = BatchPayoutRequest(
        batch_name="Split Row Attempt",
        source_owner=WALLET_A,
        token_price_usd=1.0,
        recipients=[
            PayoutRecipient(wallet=WALLET_B, amount_tokens="6000", label="Part 1"),
            PayoutRecipient(wallet=WALLET_B, amount_tokens="6000", label="Part 2"),
        ],
    )
    split_decision = evaluate_treasury_policy(split_req, max_single_recipient_usd=10_000.0)
    assert split_decision.allowed is False
    assert any("consolidated" in v for v in split_decision.violations)


def test_custom_route_validation_rules_mh018_and_mh019():
    # Contracts bundle cannot be last hop for multi-destination; sender cannot be a hop
    bad_route = CustomRoute(
        hops=[
            CustomHop(wallet=WALLET_A, waitSeconds=120),
            CustomHop(bundle="cex", waitSeconds=180),
            CustomHop(bundle="contracts", waitSeconds=300),
        ]
    )
    problems, total_wait = validate_custom_route(
        bad_route,
        for_destinations=True,
        source_owner=WALLET_A,
        destination_wallets=[WALLET_B, WALLET_C],
    )
    assert total_wait == 600
    assert any("sourceOwner" in p for p in problems)
    assert any("contracts" in p for p in problems)

    # Devnet rejects bundle hops with MH_019
    with pytest.raises(ValueError, match="MH_019"):
        validate_custom_route(bad_route, for_destinations=True, is_devnet=True)


def test_batch_status_aggregation_handles_partial_failure():
    assert (
        aggregate_batch_status([{"status": "completed"}, {"status": "failed"}])
        == "partial_failed"
    )
    assert (
        aggregate_batch_status([{"status": "completed"}, {"status": "completed"}])
        == "completed"
    )
    assert (
        aggregate_batch_status([{"status": "quote"}], requires_approval=True, approved_at="")
        == "awaiting_approval"
    )


@pytest.mark.anyio
async def test_live_http_client_contract_headers_custom_route_and_mh041():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/api/v1/transfers" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            if "routeTemplateId" in body:
                # Official rule: hops and arrivalSeconds MUST NOT be sent with routeTemplateId
                assert "hops" not in body
                assert "arrivalSeconds" not in body
            return httpx.Response(200, json={"id": 42, "status": "quote", "amountRaw": "1000000000"})
        if request.url.path == "/api/v1/transfers/42/confirm-broadcast":
            return httpx.Response(
                409,
                json={"error": {"code": "MH_041", "message": "Destinations not yet bound on chain"}},
            )
        if request.url.path == "/api/v1/route-templates" and request.method == "POST":
            return httpx.Response(200, json={"id": 7, "name": "treasury-tpl"})
        return httpx.Response(404, json={"error": {"code": "MH_030", "message": "Transfer not found"}})

    client = MultiHopperClient(
        env="devnet",
        api_key="mh_test_livecontract",
        transport=httpx.MockTransport(handler),
    )
    tr = await client.create_transfer(
        token_mint=SOL_MINT,
        amount_raw="1000000000",
        amount_tokens="1",
        token_decimals=9,
        token_symbol="SOL",
        source_owner=WALLET_A,
        destinations=[
            {"wallet": WALLET_B, "amountRaw": "600000000"},
            {"wallet": WALLET_C, "amountRaw": "400000000"},
        ],
        route_template_id=7,
        external_id="hp_test_c01",
    )
    assert tr["id"] == 42
    assert seen_requests[0].headers["x-api-key"] == "mh_test_livecontract"
    assert seen_requests[0].headers["idempotency-key"] == "hp_test_c01"

    tpl = await client.create_route_template(
        name="treasury-tpl",
        route={"hops": [{"wallet": WALLET_D, "waitSeconds": 120}, {"wallet": WALLET_B, "waitSeconds": 180}]},
    )
    assert tpl["id"] == 7
    assert "idempotency-key" in seen_requests[1].headers

    with pytest.raises(MultiHopperAPIError) as exc_info:
        await client.confirm_broadcast(42, route_init_signatures=["sig1"])
    assert exc_info.value.code == "MH_041"


def test_end_to_end_fastapi_batch_prepare_confirm_and_hmac_webhook(tmp_path: Path):
    secret = "whsec_unit_test_secret"
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "hoppay.db"),
        webhook_secret=secret,
    )
    tc = TestClient(app)

    assert tc.get("/").status_code == 200
    assert tc.get("/api/health").json()["ok"] is True

    batch_resp = tc.post(
        "/api/batches",
        json={
            "batch_name": "Sprint 1 Payroll",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "hops": 3,
            "arrival_seconds": 600,
            "recipients": [
                {"wallet": WALLET_B, "amount_tokens": "2.0", "label": "Alice"},
                {"wallet": WALLET_C, "amount_tokens": "1.0", "label": "Bob"},
            ],
        },
    )
    assert batch_resp.status_code == 200
    batch = batch_resp.json()["batch"]
    assert batch["total_usd"] == 450.0
    assert batch["estimated_integrator_rev_usd"] == 1.125  # 0.25% of $450
    tid = batch["transfers"][0]["id"]

    # Prepare updates persisted transfer status to awaiting_signature
    prep = tc.post(f"/api/transfers/{tid}/prepare")
    assert prep.status_code == 200
    assert len(prep.json()["preparedTxs"]["dispersionInitTxs"]) == 3
    assert tc.get("/api/overview").json()["batches"][0]["status"] == "awaiting_signature"

    # Two-stage confirm-broadcast (keeperFundingTx first, then full bundle)
    c1 = tc.post(
        f"/api/transfers/{tid}/confirm-broadcast",
        json={"route_init_signatures": [], "keeper_funding_signature": "sig_keep_1"},
    )
    assert c1.status_code == 200
    assert c1.json()["transfer"]["status"] == "funding_confirmed"
    assert tc.get("/api/overview").json()["batches"][0]["status"] == "funding_confirmed"

    # Missing dispersion signatures must trigger MH_041 (409)
    c_missing = tc.post(
        f"/api/transfers/{tid}/confirm-broadcast",
        json={"route_init_signatures": ["sig_route_1"], "keeper_funding_signature": "sig_keep_1"},
    )
    assert c_missing.status_code == 409

    c2 = tc.post(
        f"/api/transfers/{tid}/confirm-broadcast",
        json={
            "route_init_signatures": ["sig_route_1"],
            "keeper_funding_signature": "sig_keep_1",
            "orchestrator_init_signature": "sig_orch_1",
            "session_init_signatures": ["sig_s1", "sig_s2", "sig_s3"],
            "dispersion_init_signatures": ["sig_d1", "sig_d2", "sig_d3"],
        },
    )
    assert c2.status_code == 200
    assert c2.json()["transfer"]["status"] == "processing"

    # Webhook rejects invalid HMAC signature
    wh_payload = json.dumps({"event": "transfer.completed", "data": {"transferId": tid}}).encode("utf-8")
    bad = tc.post("/webhooks/multihopper", content=wh_payload, headers={"x-multihopper-signature": "deadbeef"})
    assert bad.status_code == 401

    # Valid HMAC webhook marks batch completed and deduplicates replays
    valid_sig = sign_webhook_payload(wh_payload, secret)
    ok1 = tc.post("/webhooks/multihopper", content=wh_payload, headers={"x-multihopper-signature": valid_sig})
    assert ok1.status_code == 200
    assert ok1.json()["duplicate"] is False

    ok2 = tc.post("/webhooks/multihopper", content=wh_payload, headers={"x-multihopper-signature": valid_sig})
    assert ok2.status_code == 200
    assert ok2.json()["duplicate"] is True

    # Integrator payout.completed webhook increments claim counter
    payout_body = json.dumps({"event": "payout.completed", "data": {"amountRaw": "2500000"}}).encode("utf-8")
    p_sig = sign_webhook_payload(payout_body, secret)
    assert tc.post("/webhooks/multihopper", content=payout_body, headers={"x-multihopper-signature": p_sig}).status_code == 200

    ov = tc.get("/api/overview").json()
    assert ov["batches"][0]["status"] == "completed"
    assert ov["summary"]["settled_volume_usd"] == 450.0
    assert ov["summary"]["payout_claims_count"] == 1


def test_human_approval_gate_blocks_prepare_until_approved_and_reject_blocks_permanently(tmp_path: Path):
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "approval.db"),
        webhook_secret="whsec_approval",
    )
    tc = TestClient(app)

    # Batch worth $3,000 exceeds default $2,500 approval threshold
    resp = tc.post(
        "/api/batches",
        json={
            "batch_name": "Treasury Vendor Payout",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "recipients": [{"wallet": WALLET_B, "amount_tokens": "20", "label": "Audit"}],
        },
    )
    assert resp.status_code == 200
    batch = resp.json()["batch"]
    assert batch["status"] == "awaiting_approval"
    tid = batch["transfers"][0]["id"]

    # Preparing before approval MUST return 409 HOPPAY_APPROVAL_REQUIRED
    blocked = tc.post(f"/api/transfers/{tid}/prepare")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "HOPPAY_APPROVAL_REQUIRED"

    # Approve batch -> prepare now succeeds
    appr = tc.post(f"/api/batches/{batch['id']}/approve", json={"approver": "DAO Multisig Lead"})
    assert appr.status_code == 200
    assert appr.json()["batch"]["status"] == "approved"
    assert appr.json()["batch"]["approved_by"] == "DAO Multisig Lead"

    prep = tc.post(f"/api/transfers/{tid}/prepare")
    assert prep.status_code == 200

    # Create a second batch and reject it -> prepare must stay permanently blocked
    resp2 = tc.post(
        "/api/batches",
        json={
            "batch_name": "Suspicious Payout",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "recipients": [{"wallet": WALLET_C, "amount_tokens": "20", "label": "Unknown"}],
        },
    )
    b2 = resp2.json()["batch"]
    tc.post(f"/api/batches/{b2['id']}/reject", json={"reason": "Unverified invoice"})
    rej_prep = tc.post(f"/api/transfers/{b2['transfers'][0]['id']}/prepare")
    assert rej_prep.status_code == 409
    assert rej_prep.json()["detail"]["code"] == "HOPPAY_BATCH_REJECTED"


def test_custom_route_templates_hop_library_simulate_settle_and_exports(tmp_path: Path):
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "templates.db"),
        webhook_secret="whsec_templates",
    )
    tc = TestClient(app)

    # 1. Browse Hop Library
    lib = tc.get("/api/hop-library?bundle=cex")
    assert lib.status_code == 200
    assert len(lib.json()["wallets"]) >= 2

    # 2. Create custom route template & check duplicate MH_022
    tpl_payload = {
        "name": "agency-3hop",
        "description": "CEX -> Custom Wallet -> KOL",
        "route": {
            "hops": [
                {"bundle": "cex", "waitSeconds": 120},
                {"wallet": WALLET_D, "waitSeconds": 180},
                {"bundle": "kol", "waitSeconds": 240},
            ]
        },
    }
    created = tc.post("/api/route-templates", json=tpl_payload)
    assert created.status_code == 200
    tpl_id = created.json()["id"]
    assert created.json()["arrivalSeconds"] == 540
    assert created.json()["usableFor"]["destinations"] is True

    dup = tc.post("/api/route-templates", json=tpl_payload)
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "MH_022"

    # 3. Estimate with routeTemplateId dry-run
    est = tc.post(
        "/api/estimate",
        json={
            "token_mint": SOL_MINT,
            "amount_raw": "3000000000",
            "token_decimals": 9,
            "destination_count": 2,
            "route_template_id": tpl_id,
            "token_price_usd": 150.0,
        },
    )
    assert est.status_code == 200
    assert est.json()["route"] == {"hopCount": 3, "arrivalSeconds": 540}

    # 4. Create batch using saved template -> simulate full settle -> verify receipt exports & MH_035
    b_resp = tc.post(
        "/api/batches",
        json={
            "batch_name": "Template Routed Payout",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "route_template_id": tpl_id,
            "recipients": [
                {"wallet": WALLET_B, "amount_tokens": "1.5", "label": "Alice"},
                {"wallet": WALLET_C, "amount_tokens": "1.0", "label": "Bob"},
            ],
        },
    )
    assert b_resp.status_code == 200
    batch = b_resp.json()["batch"]
    assert batch["routing_mode"] == "route_template"
    tid = batch["transfers"][0]["id"]
    assert len(batch["transfers"][0]["route"]["hops"]) == 3

    sim = tc.post(f"/api/demo/transfers/{tid}/simulate-settle")
    assert sim.status_code == 200
    assert sim.json()["transfer"]["status"] == "completed"

    # Poll live transfer status & confirm MH_035 blocks re-preparing a completed transfer
    polled = tc.get(f"/api/transfers/{tid}")
    assert polled.status_code == 200
    assert polled.json()["progress"] == {"hopsCompleted": 3, "hopsTotal": 3}

    reprep = tc.post(f"/api/transfers/{tid}/prepare")
    assert reprep.status_code == 409
    assert reprep.json()["detail"]["code"] == "MH_035"

    # Export CSV & JSON audit receipts
    csv_res = tc.get(f"/api/batches/{batch['id']}/export.csv")
    assert csv_res.status_code == 200
    assert WALLET_B in csv_res.text and "paid" in csv_res.text

    json_res = tc.get(f"/api/batches/{batch['id']}/export.json")
    assert json_res.status_code == 200
    assert json_res.json()["batch"]["status"] == "completed"

    # Delete template succeeds
    assert tc.delete(f"/api/route-templates/{tpl_id}").status_code == 200


def test_demo_simulate_endpoint_disabled_in_live_mode(tmp_path: Path):
    live_client = MultiHopperClient(env="devnet", api_key="mh_test_livekey")
    app = create_app(
        client=live_client,
        store=HopPayStore(tmp_path / "live.db"),
        webhook_secret="whsec_live",
    )
    tc = TestClient(app)
    res = tc.post("/api/demo/transfers/101/simulate-settle")
    assert res.status_code == 403


def test_21_wallet_batch_with_10_hop_custom_route_chunks_uniformly_without_mh018(tmp_path: Path):
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "chunk10.db"),
        webhook_secret="whsec_chunk10",
    )
    tc = TestClient(app)
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    recipients = [
        {
            "wallet": f"7pBqizvE8vQZ29F49NWSu9cho3s7M9SqJyNN1UoQ{alphabet[i]}{alphabet[i+1]}",
            "amount_tokens": "10",
            "label": f"Member {i+1}",
        }
        for i in range(21)
    ]
    # 10 hops is valid ONLY for destinations mode (3-10 hops), NOT single recipientWallet (2-9 hops)
    ten_hops = [
        {"wallet": f"3UdgSTpcUbiBSmgdPBiVHRMbcDS731kMT6bAQQZg{alphabet[i+30]}{alphabet[i+31]}", "waitSeconds": 60}
        for i in range(10)
    ]
    resp = tc.post(
        "/api/batches",
        json={
            "batch_name": "21-Wallet 10-Hop Route",
            "business_purpose": "DAO Milestone Grants",
            "source_owner": WALLET_A,
            "token_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "token_symbol": "USDC",
            "token_decimals": 6,
            "token_price_usd": 1.0,
            "route": {"hops": ten_hops},
            "idempotency_key": "dao-milestone-2026-11",
            "recipients": recipients,
        },
    )
    assert resp.status_code == 200, resp.text
    batch = resp.json()["batch"]
    assert batch["chunk_count"] == 2
    assert batch["transfers"][0]["destinationCount"] == 20
    assert batch["transfers"][1]["destinationCount"] == 1
    assert batch["transfers"][1]["recipientWallet"] is None
    assert batch["business_purpose"] == "DAO Milestone Grants"

    # Idempotent replay returns exact same batch without creating duplicate transfers
    replay = tc.post(
        "/api/batches",
        json={
            "batch_name": "21-Wallet 10-Hop Route",
            "business_purpose": "DAO Milestone Grants",
            "source_owner": WALLET_A,
            "token_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "token_symbol": "USDC",
            "token_decimals": 6,
            "token_price_usd": 1.0,
            "route": {"hops": ten_hops},
            "idempotency_key": "dao-milestone-2026-11",
            "recipients": recipients,
        },
    )
    assert replay.status_code == 200
    assert replay.json().get("idempotent_replay") is True
    assert replay.json()["batch"]["id"] == batch["id"]


def test_operator_admin_auth_guards_mutations_and_allows_bearer_or_header(tmp_path: Path):
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "auth.db"),
        webhook_secret="whsec_auth",
        admin_token="hoppay_secret_ops_token_2026",
    )
    tc = TestClient(app)

    assert tc.get("/api/health").json()["auth_enabled"] is True

    payload = {
        "batch_name": "Protected Payroll",
        "source_owner": WALLET_A,
        "token_mint": SOL_MINT,
        "token_symbol": "SOL",
        "token_decimals": 9,
        "token_price_usd": 150.0,
        "recipients": [{"wallet": WALLET_B, "amount_tokens": "1.0", "label": "Dev"}],
    }
    assert tc.post("/api/batches", json=payload).status_code == 401
    assert tc.post("/api/batches", json=payload, headers={"x-hoppay-admin-key": "wrong"}).status_code == 401
    assert tc.get("/api/webhooks").status_code == 401

    ok = tc.post(
        "/api/batches",
        json=payload,
        headers={"Authorization": "Bearer hoppay_secret_ops_token_2026"},
    )
    assert ok.status_code == 200
    assert tc.get("/api/webhooks", headers={"x-hoppay-admin-key": "hoppay_secret_ops_token_2026"}).status_code == 200


def test_demo_mode_hydrates_from_sqlite_across_server_restart(tmp_path: Path):
    db_file = tmp_path / "restart.db"
    app1 = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(db_file),
        webhook_secret="whsec_restart",
    )
    tc1 = TestClient(app1)
    created = tc1.post(
        "/api/batches",
        json={
            "batch_name": "Pre-Restart Payroll",
            "business_purpose": "Core Engineering Sprint",
            "source_owner": WALLET_A,
            "token_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "token_symbol": "USDC",
            "token_decimals": 6,
            "token_price_usd": 1.0,
            "recipients": [{"wallet": WALLET_B, "amount_tokens": "500", "label": "Single Dev"}],
        },
    )
    assert created.status_code == 200
    batch_id = created.json()["batch"]["id"]
    tid = created.json()["batch"]["transfers"][0]["id"]

    # Simulate full process restart with brand new MultiHopperClient reading the same SQLite DB
    app2 = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(db_file),
        webhook_secret="whsec_restart",
    )
    tc2 = TestClient(app2)
    sim = tc2.post(f"/api/demo/transfers/{tid}/simulate-settle")
    assert sim.status_code == 200
    assert sim.json()["transfer"]["status"] == "completed"

    # Single-recipient CSV receipt includes settlement signature + business purpose
    csv_res = tc2.get(f"/api/batches/{batch_id}/export.csv")
    assert csv_res.status_code == 200
    assert "Core Engineering Sprint" in csv_res.text
    final_hop_sig = sim.json()["transfer"]["signatures"]["hops"][-1]
    assert final_hop_sig in csv_res.text

    # Qualifying volume report includes settled batch evidence and SPL usage fees
    rep = tc2.get("/api/reports/qualifying-volume")
    assert rep.status_code == 200
    rep_json = rep.json()
    assert rep_json["is_mainnet_qualifying"] is False
    assert rep_json["local_ledger_summary"]["settled_volume_usd"] == 500.0
    assert len(rep_json["settled_batch_evidence"]) == 1
    assert rep_json["multihopper_api_usage"]["completedTransfers"] == 1
    assert len(rep_json["multihopper_api_usage"]["tokenFeesEarned"]) == 1


def test_mh040_tier_shift_requote_partial_chunk_safety_and_resilient_overview(tmp_path: Path):
    create_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/usage":
            return httpx.Response(503, json={"error": {"code": "MH_091", "message": "Service temporarily unavailable"}})
        if request.url.path == "/api/v1/transfers" and request.method == "POST":
            create_calls["count"] += 1
            body = json.loads(request.content.decode("utf-8"))
            if body.get("externalId", "").endswith("_c02"):
                return httpx.Response(429, headers={"Retry-After": "15"}, json={"error": {"code": "MH_004", "message": "Rate limit exceeded"}})
            return httpx.Response(
                200,
                json={
                    "id": 500 + create_calls["count"],
                    "externalId": body.get("externalId"),
                    "supportBundleId": f"MH-live-{500 + create_calls['count']}",
                    "tokenMint": body["tokenMint"],
                    "amountRaw": body["amountRaw"],
                    "amountTokens": body["amountTokens"],
                    "sourceOwner": body["sourceOwner"],
                    "recipientWallet": body.get("recipientWallet"),
                    "destinations": body.get("destinations"),
                    "hops": body.get("hops", 3),
                    "arrivalSeconds": body.get("arrivalSeconds", 600),
                    "status": "quote",
                },
            )
        if request.url.path == "/api/v1/transfers/501/prepare":
            return httpx.Response(
                409,
                json={"error": {"code": "MH_040", "message": "Tier shifted since quote"}},
            )
        return httpx.Response(404, json={"error": {"code": "MH_030", "message": "Not found"}})

    live_mh = MultiHopperClient(
        env="mainnet",
        api_key="mh_live_test_key",
        transport=httpx.MockTransport(handler),
    )
    app = create_app(
        client=live_mh,
        store=HopPayStore(tmp_path / "live_mh040.db"),
        webhook_secret="whsec_mh040",
    )
    tc = TestClient(app)

    # 1. Overview gracefully degrades when /usage returns 503 instead of crashing with 500
    ov = tc.get("/api/overview")
    assert ov.status_code == 200
    assert ov.json()["usage"] is None
    assert "MH_091" in ov.json()["usage_error"]
    assert ov.json()["security_warning"] is not None

    # 2. Single-chunk batch created -> /prepare hits MH_040 -> /requote creates transfer #502 & archives #501
    b1 = tc.post(
        "/api/batches",
        json={
            "batch_name": "Live Requote Batch",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "recipients": [{"wallet": WALLET_B, "amount_tokens": "1.0"}],
        },
    )
    assert b1.status_code == 200
    bid = b1.json()["batch"]["id"]
    prep = tc.post("/api/transfers/501/prepare")
    assert prep.status_code == 409
    assert prep.json()["detail"]["code"] == "MH_040"
    assert prep.json()["detail"]["requote_available"] is True

    req = tc.post(f"/api/batches/{bid}/transfers/501/requote")
    assert req.status_code == 200
    assert req.json()["new_transfer"]["id"] == 502
    assert req.json()["new_transfer"]["externalId"].endswith("_r1")
    assert len(req.json()["batch"]["route_config"]["superseded_transfers"]) == 1

    # 3. Partial chunk failure on Chunk 2 persists Chunk 1 in SQLite so no live transfer is orphaned
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    recs21 = [
        {"wallet": f"7pBqizvE8vQZ29F49NWSu9cho3s7M9SqJyNN1UoQ{alphabet[i]}{alphabet[i+1]}", "amount_tokens": "0.1"}
        for i in range(21)
    ]
    partial = tc.post(
        "/api/batches",
        json={
            "batch_name": "Partial Chunk Safety",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "recipients": recs21,
        },
    )
    assert partial.status_code == 429
    partial_bid = partial.json()["detail"]["partial_batch_id"]
    assert partial_bid is not None
    persisted = tc.get(f"/api/batches/{partial_bid}/export.json")
    assert persisted.status_code == 200
    assert persisted.json()["batch"]["status"] == "partial_failed"
    assert len(persisted.json()["batch"]["transfers"]) == 1


def test_route_template_patch_exact_csv_summary_and_external_lookup(tmp_path: Path):
    app = create_app(
        client=MultiHopperClient(env="demo"),
        store=HopPayStore(tmp_path / "crud.db"),
        webhook_secret="whsec_crud",
    )
    tc = TestClient(app)

    # Exact Decimal CSV summary avoids JS floating point error (0.1 + 0.2 == 0.300000000 exact)
    parsed = tc.post(
        "/api/csv/parse",
        json={
            "csv_text": f"wallet,amount,label\n{WALLET_B},0.1,A\n{WALLET_C},0.2,B\n",
            "token_decimals": 9,
        },
    )
    assert parsed.status_code == 200
    assert parsed.json()["summary"]["total_raw"] == "300000000"
    assert parsed.json()["summary"]["total_tokens"] == "0.3"

    # PATCH route template updates name & description
    tpl = tc.get("/api/route-templates/1")
    assert tpl.status_code == 200
    patched = tc.patch(
        "/api/route-templates/1",
        json={"name": "dao-payroll-3hop-v2", "description": "Updated treasury route"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "dao-payroll-3hop-v2"

    # Create batch and look up transfer by externalId
    b = tc.post(
        "/api/batches",
        json={
            "batch_name": "Ext Lookup Test",
            "source_owner": WALLET_A,
            "token_mint": SOL_MINT,
            "token_symbol": "SOL",
            "token_decimals": 9,
            "token_price_usd": 150.0,
            "recipients": [{"wallet": WALLET_B, "amount_tokens": "1.0"}],
        },
    )
    ext_id = b.json()["batch"]["transfers"][0]["externalId"]
    looked = tc.get(f"/api/transfers/by-external/{ext_id}")
    assert looked.status_code == 200
    assert looked.json()["externalId"] == ext_id

