"""FastAPI backend + Interactive UI for HopPay Treasury (MultiHopper Build & Route Challenge)."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from hoppay.client import MultiHopperAPIError, MultiHopperClient
from hoppay.models import (
    SOL_MINT,
    BatchPayoutRequest,
    EstimateRequest,
    RouteTemplateCreate,
    RouteTemplateUpdate,
    chunk_recipients_for_multihopper,
    evaluate_treasury_policy,
    parse_payout_csv,
    raw_to_tokens,
    should_use_destinations,
    summarize_recipients_exact,
)
from hoppay.store import HopPayStore, sign_webhook_payload, verify_multihopper_signature

STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
APP_VERSION = "1.0.0"


class CsvParseRequest(BaseModel):
    csv_text: str
    token_decimals: int = Field(default=9, ge=0, le=18)


class ConfirmBroadcastRequest(BaseModel):
    route_init_signatures: list[str]
    keeper_funding_signature: str | None = None
    orchestrator_init_signature: str | None = None
    session_init_signatures: list[str] | None = None
    dispersion_init_signatures: list[str] | None = None


class ApproveBatchRequest(BaseModel):
    approver: str = Field(default="Treasury Lead", min_length=1, max_length=80)


class RejectBatchRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


class WebhookRegisterRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=500)
    events: list[str] | None = None


def _explorer_tx_url(sig: str, env: str) -> str:
    if not sig or sig.startswith(("demo_", "wh_settled_")):
        return ""
    suffix = "" if env == "mainnet" else "?cluster=devnet"
    return f"https://explorer.solana.com/tx/{sig}{suffix}"


def _derive_batch_id(idempotency_key: str | None) -> str:
    if idempotency_key and idempotency_key.strip():
        cleaned = SAFE_ID_RE.sub("_", idempotency_key.strip()).strip("._-")
        if cleaned:
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
            return f"hp_{digest}"
    return f"hp_{uuid.uuid4().hex[:10]}"


def create_app(
    *,
    client: MultiHopperClient | None = None,
    store: HopPayStore | None = None,
    webhook_secret: str | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    env = os.environ.get("MULTIHOPPER_ENV", "demo")
    api_key = os.environ.get("MULTIHOPPER_API_KEY", "")
    wh_secret = (
        webhook_secret
        if webhook_secret is not None
        else os.environ.get("MULTIHOPPER_WEBHOOK_SECRET", "whsec_dev_local_change_me")
    )
    configured_admin_token = (
        admin_token.strip()
        if admin_token is not None
        else os.environ.get("HOPPAY_ADMIN_TOKEN", "").strip()
    )
    max_single_usd = float(os.environ.get("HOPPAY_MAX_SINGLE_RECIPIENT_USD", "10000"))
    approval_usd = float(os.environ.get("HOPPAY_APPROVAL_THRESHOLD_USD", "2500"))
    db_path = Path(
        os.environ.get("HOPPAY_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "hoppay.db"))
    )
    default_rpc = (
        "https://api.mainnet-beta.solana.com"
        if env.lower() == "mainnet"
        else "https://api.devnet.solana.com"
    )
    solana_rpc_url = os.environ.get("SOLANA_RPC_URL", default_rpc)

    mh = client or MultiHopperClient(env=env, api_key=api_key)
    db = store or HopPayStore(db_path)
    if mh.is_demo:
        mh.hydrate_demo_transfers(db.list_batches(limit=500))

    def require_operator(request: Request) -> None:
        if not configured_admin_token:
            return
        supplied = (request.headers.get("x-hoppay-admin-key") or "").strip()
        if not supplied:
            auth = (request.headers.get("authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, configured_admin_token):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "HOPPAY_UNAUTHORIZED",
                    "message": "Operator authentication required. Supply x-hoppay-admin-key or Authorization: Bearer <HOPPAY_ADMIN_TOKEN>.",
                },
            )

    security_warning: str | None = None
    if not mh.is_demo and not configured_admin_token:
        security_warning = (
            "Live MultiHopper mode is running without HOPPAY_ADMIN_TOKEN. "
            "Set HOPPAY_ADMIN_TOKEN before exposing this server publicly."
        )

    app = FastAPI(title="HopPay Treasury — MultiHopper Solana Router", version=APP_VERSION)
    app.state.mh = mh
    app.state.db = db

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hoppay-treasury",
            "version": APP_VERSION,
            "env": mh.env,
            "is_demo_mode": mh.is_demo,
            "auth_enabled": bool(configured_admin_token),
            "security_warning": security_warning,
            "base_url": mh.base_url,
        }

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        if not STATIC_INDEX.exists():
            return HTMLResponse("<h1>HopPay Treasury</h1><p>UI file missing.</p>", status_code=500)
        return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"))

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        usage: dict[str, Any] | None = None
        usage_error: str | None = None
        try:
            usage = await mh.get_usage()
        except (MultiHopperAPIError, httpx.HTTPError) as exc:
            usage_error = str(exc)
        return {
            "version": APP_VERSION,
            "env": mh.env,
            "is_demo_mode": mh.is_demo,
            "auth_enabled": bool(configured_admin_token),
            "security_warning": security_warning,
            "base_url": mh.base_url,
            "solana_rpc_url": solana_rpc_url,
            "explorer_cluster": "" if mh.env == "mainnet" else "?cluster=devnet",
            "policy_config": {
                "max_single_recipient_usd": max_single_usd,
                "approval_threshold_usd": approval_usd,
            },
            "summary": db.get_summary(),
            "usage": usage,
            "usage_error": usage_error,
            "batches": db.list_batches(limit=30),
            "webhook_events": db.list_webhook_events(limit=25),
        }

    @app.post("/api/csv/parse")
    def parse_csv(req: CsvParseRequest) -> dict[str, Any]:
        try:
            recipients = parse_payout_csv(req.csv_text)
            summary = summarize_recipients_exact(recipients, req.token_decimals)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "count": len(recipients),
            "summary": summary,
            "recipients": [r.model_dump() for r in recipients],
        }

    @app.post("/api/estimate")
    async def estimate(req: EstimateRequest) -> dict[str, Any]:
        try:
            est = await mh.estimate(
                token_mint=req.token_mint,
                amount_raw=req.amount_raw,
                token_decimals=req.token_decimals,
                hops=req.hops,
                destination_count=req.destination_count,
                arrival_seconds=req.arrival_seconds,
                route=req.route.to_api_dict() if req.route else None,
                route_template_id=req.route_template_id,
                token_price_usd=req.token_price_usd,
            )
            return est
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message, "retry_after": exc.retry_after},
            )

    @app.get("/api/hop-library")
    async def hop_library(
        bundle: str | None = None,
        category: str | None = None,
        can_end_route: bool | None = None,
    ) -> dict[str, Any]:
        try:
            return await mh.get_hop_library(
                bundle=bundle, category=category, can_end_route=can_end_route
            )
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.get("/api/route-templates")
    async def list_templates(name: str | None = None) -> dict[str, Any]:
        try:
            data = await mh.list_route_templates(name=name)
            items = data if isinstance(data, list) else data.get("items", [])
            return {"items": items}
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.get("/api/route-templates/{template_id}")
    async def get_template(template_id: int) -> dict[str, Any]:
        try:
            return await mh.get_route_template(template_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.post("/api/route-templates", dependencies=[Depends(require_operator)])
    async def create_template(req: RouteTemplateCreate) -> dict[str, Any]:
        try:
            return await mh.create_route_template(
                name=req.name.strip(),
                route=req.route.to_api_dict(),
                description=req.description,
            )
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.patch("/api/route-templates/{template_id}", dependencies=[Depends(require_operator)])
    async def update_template(template_id: int, req: RouteTemplateUpdate) -> dict[str, Any]:
        try:
            return await mh.update_route_template(
                template_id,
                name=req.name.strip() if req.name is not None else None,
                route=req.route.to_api_dict() if req.route is not None else None,
                description=req.description,
            )
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.delete("/api/route-templates/{template_id}", dependencies=[Depends(require_operator)])
    async def delete_template(template_id: int) -> dict[str, Any]:
        try:
            return await mh.delete_route_template(template_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.get("/api/webhooks", dependencies=[Depends(require_operator)])
    async def list_webhooks() -> dict[str, Any]:
        try:
            data = await mh.list_webhooks()
            items = data if isinstance(data, list) else data.get("items", [])
            return {"items": items, "is_demo_mode": mh.is_demo}
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.post("/api/webhooks", dependencies=[Depends(require_operator)])
    async def register_webhook(req: WebhookRegisterRequest) -> dict[str, Any]:
        if not req.url.startswith("https://") and not mh.is_demo:
            raise HTTPException(
                status_code=400,
                detail={"code": "HOPPAY_INVALID_WEBHOOK_URL", "message": "Live MultiHopper webhooks require an https:// URL."},
            )
        try:
            return await mh.register_webhook(url=req.url.strip(), events=req.events)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.delete("/api/webhooks/{webhook_id}", dependencies=[Depends(require_operator)])
    async def delete_webhook(webhook_id: str) -> dict[str, Any]:
        try:
            return await mh.delete_webhook(webhook_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.post("/api/batches", dependencies=[Depends(require_operator)])
    async def create_batch(req: BatchPayoutRequest) -> dict[str, Any]:
        policy = evaluate_treasury_policy(
            req,
            max_single_recipient_usd=max_single_usd,
            approval_threshold_usd=approval_usd,
            is_devnet=(mh.env == "devnet"),
        )
        if not policy.allowed:
            raise HTTPException(status_code=400, detail={"policy": policy.model_dump()})

        is_sol = req.token_mint == SOL_MINT
        try:
            chunks = chunk_recipients_for_multihopper(
                req.recipients,
                decimals=req.token_decimals,
                is_sol=is_sol,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        batch_id = _derive_batch_id(req.idempotency_key)
        existing_batch = db.get_batch(batch_id) if req.idempotency_key else None
        if existing_batch and existing_batch["status"] != "partial_failed":
            return {"batch": existing_batch, "policy": policy.model_dump(), "idempotent_replay": True}

        created_transfers: list[dict[str, Any]] = []
        chunk_specs: dict[str, dict[str, Any]] = {}
        route_dict = req.route.to_api_dict() if req.route else None
        routing_mode = (
            "custom_route"
            if route_dict is not None
            else ("route_template" if req.route_template_id is not None else "standard")
        )

        for idx, dests in enumerate(chunks, start=1):
            chunk_raw = sum(int(d["amountRaw"]) for d in dests)
            chunk_tokens = raw_to_tokens(chunk_raw, req.token_decimals)
            ext_id = f"{batch_id}_c{idx:02d}"
            use_dests = should_use_destinations(
                chunk_len=len(dests),
                total_chunks=len(chunks),
                route=req.route,
            )
            chunk_specs[ext_id] = {
                "amount_raw": str(chunk_raw),
                "amount_tokens": chunk_tokens,
                "use_destinations": use_dests,
                "destinations": dests if use_dests else None,
                "recipient_wallet": None if use_dests else dests[0]["wallet"],
                "requote_count": 0,
            }
            try:
                if not use_dests:
                    tr = await mh.create_transfer(
                        token_mint=req.token_mint,
                        amount_raw=str(chunk_raw),
                        amount_tokens=chunk_tokens,
                        token_decimals=req.token_decimals,
                        token_symbol=req.token_symbol,
                        source_owner=req.source_owner,
                        hops=req.hops,
                        arrival_seconds=req.arrival_seconds,
                        recipient_wallet=dests[0]["wallet"],
                        route=route_dict,
                        route_template_id=req.route_template_id,
                        external_id=ext_id,
                        token_price_usd=req.token_price_usd,
                    )
                else:
                    tr = await mh.create_transfer(
                        token_mint=req.token_mint,
                        amount_raw=str(chunk_raw),
                        amount_tokens=chunk_tokens,
                        token_decimals=req.token_decimals,
                        token_symbol=req.token_symbol,
                        source_owner=req.source_owner,
                        hops=req.hops,
                        arrival_seconds=req.arrival_seconds,
                        destinations=dests,
                        route=route_dict,
                        route_template_id=req.route_template_id,
                        external_id=ext_id,
                        token_price_usd=req.token_price_usd,
                    )
            except MultiHopperAPIError as exc:
                if created_transfers:
                    # Persist already-created MultiHopper transfers so nothing is orphaned from audit ledger
                    est_rev_usd = round(policy.total_usd * 0.0025, 4)
                    db.save_batch(
                        batch_id=batch_id,
                        batch_name=req.batch_name,
                        business_purpose=req.business_purpose,
                        source_owner=req.source_owner,
                        token_mint=req.token_mint,
                        token_symbol=req.token_symbol,
                        token_decimals=req.token_decimals,
                        recipient_count=policy.unique_recipient_count or len(req.recipients),
                        chunk_count=len(chunks),
                        total_usd=policy.total_usd,
                        estimated_integrator_rev_usd=est_rev_usd,
                        status="partial_failed",
                        requires_approval=policy.requires_human_approval,
                        transfers=created_transfers,
                        routing_mode=routing_mode,
                        route_config={
                            "hops": req.hops,
                            "arrival_seconds": req.arrival_seconds,
                            "token_price_usd": req.token_price_usd,
                            "route": route_dict,
                            "route_template_id": req.route_template_id,
                            "chunk_specs": chunk_specs,
                            "chunk_creation_error": {
                                "failed_chunk": idx,
                                "code": exc.code,
                                "message": exc.message,
                            },
                        },
                    )
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "code": exc.code,
                        "message": exc.message,
                        "partial_batch_id": batch_id if created_transfers else None,
                    },
                )
            created_transfers.append(tr)

        est_rev_usd = round(policy.total_usd * 0.0025, 4)  # 0.25% integrator revenue share
        batch = db.save_batch(
            batch_id=batch_id,
            batch_name=req.batch_name,
            business_purpose=req.business_purpose,
            source_owner=req.source_owner,
            token_mint=req.token_mint,
            token_symbol=req.token_symbol,
            token_decimals=req.token_decimals,
            recipient_count=policy.unique_recipient_count or len(req.recipients),
            chunk_count=len(chunks),
            total_usd=policy.total_usd,
            estimated_integrator_rev_usd=est_rev_usd,
            status="awaiting_approval" if policy.requires_human_approval else "quoted",
            requires_approval=policy.requires_human_approval,
            transfers=created_transfers,
            routing_mode=routing_mode,
            route_config={
                "hops": req.hops,
                "arrival_seconds": req.arrival_seconds,
                "token_price_usd": req.token_price_usd,
                "route": route_dict,
                "route_template_id": req.route_template_id,
                "chunk_specs": chunk_specs,
            },
        )
        return {"batch": batch, "policy": policy.model_dump()}

    @app.post("/api/batches/{batch_id}/approve", dependencies=[Depends(require_operator)])
    def approve_batch(batch_id: str, req: ApproveBatchRequest) -> dict[str, Any]:
        try:
            batch = db.approve_batch(batch_id, req.approver)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
        return {"batch": batch}

    @app.post("/api/batches/{batch_id}/reject", dependencies=[Depends(require_operator)])
    def reject_batch(batch_id: str, req: RejectBatchRequest) -> dict[str, Any]:
        batch = db.reject_batch(batch_id, req.reason)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
        return {"batch": batch}

    @app.post(
        "/api/batches/{batch_id}/transfers/{transfer_id}/requote",
        dependencies=[Depends(require_operator)],
    )
    async def requote_transfer(batch_id: str, transfer_id: int) -> dict[str, Any]:
        """Re-create a transfer after MH_040 (on-chain fee tier shift) or MH_034 (quote expired)."""
        batch = db.get_batch(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
        if batch["status"] == "rejected":
            raise HTTPException(status_code=409, detail={"code": "HOPPAY_BATCH_REJECTED", "message": "Rejected batch cannot be re-quoted."})

        target = next((t for t in batch["transfers"] if int(t.get("id", -1)) == transfer_id), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"Transfer {transfer_id} not in batch '{batch_id}'")
        if target.get("status") in ("completed", "processing", "active", "funding_confirmed"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "HOPPAY_REQUOTE_NOT_ALLOWED",
                    "message": f"Transfer #{transfer_id} is already '{target.get('status')}' and must not be re-quoted.",
                },
            )

        cfg = batch.get("route_config") or {}
        chunk_specs = cfg.get("chunk_specs") or {}
        orig_ext = str(target.get("externalId") or f"{batch_id}_t{transfer_id}")
        base_ext = orig_ext.split("_r")[0]
        spec = chunk_specs.get(base_ext) or chunk_specs.get(orig_ext) or {}
        attempt_n = int(spec.get("requote_count") or 0) + 1
        new_ext = f"{base_ext}_r{attempt_n}"

        amount_raw = str(spec.get("amount_raw") or target.get("amountRaw"))
        amount_tokens = str(spec.get("amount_tokens") or target.get("amountTokens"))
        destinations = spec.get("destinations")
        recipient_wallet = spec.get("recipient_wallet") or target.get("recipientWallet")
        if destinations is None and isinstance(target.get("destinations"), list) and target["destinations"]:
            # Fallback if batch predates chunk_specs: rescale destination shares proportionally to gross amountRaw
            raw_dests = target["destinations"]
            net_sum = sum(int(d["amountRaw"]) for d in raw_dests)
            gross_int = int(amount_raw)
            allocated = 0
            destinations = []
            for idx_d, d in enumerate(raw_dests):
                if idx_d == len(raw_dests) - 1:
                    share = gross_int - allocated
                else:
                    share = (gross_int * int(d["amountRaw"])) // max(1, net_sum)
                    allocated += share
                destinations.append({"wallet": d["wallet"], "amountRaw": str(share)})
            recipient_wallet = None

        try:
            new_tr = await mh.create_transfer(
                token_mint=batch["token_mint"],
                amount_raw=amount_raw,
                amount_tokens=amount_tokens,
                token_decimals=int(batch["token_decimals"]),
                token_symbol=batch["token_symbol"],
                source_owner=batch["source_owner"],
                hops=int(cfg.get("hops") or target.get("hops") or 3),
                arrival_seconds=int(cfg.get("arrival_seconds") or target.get("arrivalSeconds") or 600),
                recipient_wallet=recipient_wallet if not destinations else None,
                destinations=destinations,
                route=cfg.get("route"),
                route_template_id=cfg.get("route_template_id"),
                external_id=new_ext,
                token_price_usd=cfg.get("token_price_usd"),
            )
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

        if base_ext in chunk_specs:
            chunk_specs[base_ext]["requote_count"] = attempt_n
        else:
            chunk_specs[base_ext] = {
                "amount_raw": amount_raw,
                "amount_tokens": amount_tokens,
                "use_destinations": bool(destinations),
                "destinations": destinations,
                "recipient_wallet": recipient_wallet if not destinations else None,
                "requote_count": attempt_n,
            }
        updated_batch = db.replace_transfer_in_batch(
            batch_id,
            transfer_id,
            new_tr,
            reason=f"requote_r{attempt_n}",
            extra_config={"chunk_specs": chunk_specs},
        )
        return {"old_transfer_id": transfer_id, "new_transfer": new_tr, "batch": updated_batch}

    @app.get("/api/batches/{batch_id}/export.json")
    def export_batch_json(batch_id: str) -> dict[str, Any]:
        batch = db.get_batch(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
        return {
            "receipt_version": "1.1",
            "product": "HopPay Treasury (MultiHopper Build & Route)",
            "env": mh.env,
            "is_mainnet_qualifying": mh.env == "mainnet" and not mh.is_demo,
            "batch": batch,
        }

    @app.get("/api/batches/{batch_id}/export.csv", response_class=PlainTextResponse)
    def export_batch_csv(batch_id: str) -> PlainTextResponse:
        batch = db.get_batch(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "batch_id",
                "batch_name",
                "business_purpose",
                "transfer_id",
                "external_id",
                "support_bundle_id",
                "token_symbol",
                "recipient_wallet",
                "net_amount_raw",
                "net_amount_tokens",
                "destination_status",
                "payout_signature",
                "explorer_url",
                "paid_at",
                "transfer_status",
            ]
        )
        decimals = int(batch["token_decimals"])
        purpose = batch.get("business_purpose") or ""
        for t in batch["transfers"]:
            dests = t.get("destinations")
            if isinstance(dests, list) and dests:
                for d in dests:
                    psig = str(d.get("payoutSignature") or "")
                    writer.writerow(
                        [
                            batch["id"],
                            batch["batch_name"],
                            purpose,
                            t.get("id"),
                            t.get("externalId") or "",
                            t.get("supportBundleId") or "",
                            batch["token_symbol"],
                            d.get("wallet") or "",
                            d.get("amountRaw") or "",
                            raw_to_tokens(d.get("amountRaw") or "0", decimals),
                            d.get("status") or "pending",
                            psig,
                            _explorer_tx_url(psig, mh.env),
                            d.get("paidAt") or "",
                            t.get("status") or "",
                        ]
                    )
            else:
                amt = t.get("recipientReceivesRaw") or t.get("amountRaw") or "0"
                hop_sigs = (t.get("signatures") or {}).get("hops") or []
                final_sig = str(hop_sigs[-1]) if hop_sigs else ""
                paid_at = str(t.get("completedAt") or "")
                if not paid_at and isinstance(t.get("route"), dict):
                    rhops = t["route"].get("hops") or []
                    if rhops and rhops[-1].get("executedAt"):
                        paid_at = str(rhops[-1]["executedAt"])
                writer.writerow(
                    [
                        batch["id"],
                        batch["batch_name"],
                        purpose,
                        t.get("id"),
                        t.get("externalId") or "",
                        t.get("supportBundleId") or "",
                        batch["token_symbol"],
                        t.get("recipientWallet") or "",
                        amt,
                        raw_to_tokens(amt, decimals),
                        t.get("status") or "",
                        final_sig,
                        _explorer_tx_url(final_sig, mh.env),
                        paid_at,
                        t.get("status") or "",
                    ]
                )
        return PlainTextResponse(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{batch_id}_receipt.csv"'},
        )

    @app.get("/api/reports/qualifying-volume")
    async def qualifying_volume_report() -> dict[str, Any]:
        """Superteam Earn MultiHopper Build & Route Challenge qualifying-volume & audit evidence pack."""
        usage: dict[str, Any] | None = None
        usage_error: str | None = None
        try:
            usage = await mh.get_usage()
        except (MultiHopperAPIError, httpx.HTTPError) as exc:
            usage_error = str(exc)

        batches = db.list_batches(limit=500)
        settled_batches = [b for b in batches if b["status"] == "completed"]
        evidence_rows: list[dict[str, Any]] = []
        for b in settled_batches:
            tx_links: list[dict[str, str]] = []
            bundle_ids: list[str] = []
            for t in b["transfers"]:
                if t.get("supportBundleId"):
                    bundle_ids.append(str(t["supportBundleId"]))
                for d in t.get("destinations") or []:
                    sig = str(d.get("payoutSignature") or "")
                    if sig:
                        tx_links.append(
                            {
                                "transfer_id": str(t.get("id")),
                                "wallet": str(d.get("wallet") or ""),
                                "signature": sig,
                                "explorer_url": _explorer_tx_url(sig, mh.env),
                            }
                        )
                if not t.get("destinations"):
                    hs = (t.get("signatures") or {}).get("hops") or []
                    if hs:
                        sig = str(hs[-1])
                        tx_links.append(
                            {
                                "transfer_id": str(t.get("id")),
                                "wallet": str(t.get("recipientWallet") or ""),
                                "signature": sig,
                                "explorer_url": _explorer_tx_url(sig, mh.env),
                            }
                        )
            evidence_rows.append(
                {
                    "batch_id": b["id"],
                    "batch_name": b["batch_name"],
                    "business_purpose": b.get("business_purpose") or "",
                    "token_symbol": b["token_symbol"],
                    "recipient_count": b["recipient_count"],
                    "total_usd": b["total_usd"],
                    "estimated_integrator_rev_usd": b["estimated_integrator_rev_usd"],
                    "approved_by": b.get("approved_by") or None,
                    "support_bundle_ids": bundle_ids,
                    "settlement_transactions": tx_links,
                    "created_at": b["created_at"],
                }
            )

        summary = db.get_summary()
        return {
            "report_version": "1.0",
            "challenge": "MultiHopper Build & Route Challenge (Superteam Earn)",
            "env": mh.env,
            "is_mainnet_qualifying": mh.env == "mainnet" and not mh.is_demo,
            "qualifying_rule": "Only Solana Mainnet production volume counts (>= $100,000 qualifying volume = collected MultiHopper percentage fees / 0.0025).",
            "local_ledger_summary": summary,
            "multihopper_api_usage": usage,
            "multihopper_api_usage_error": usage_error,
            "settled_batch_evidence": evidence_rows,
        }

    @app.get("/api/transfers/by-external/{external_id}")
    async def get_transfer_by_external(external_id: str) -> dict[str, Any]:
        try:
            return await mh.get_transfer_by_external_id(external_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )

    @app.get("/api/transfers/{transfer_id}")
    async def get_transfer(transfer_id: int) -> dict[str, Any]:
        try:
            tr = await mh.get_transfer(transfer_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )
        db.sync_transfer_state(
            transfer_id,
            {
                "status": tr.get("status"),
                "phase": tr.get("phase"),
                "progress": tr.get("progress"),
                "destinations": tr.get("destinations"),
                "route": tr.get("route"),
                "signatures": tr.get("signatures"),
                "recovery": tr.get("recovery"),
                "lastError": tr.get("lastError"),
            },
        )
        return tr

    @app.post("/api/transfers/{transfer_id}/prepare", dependencies=[Depends(require_operator)])
    async def prepare_transfer(transfer_id: int) -> dict[str, Any]:
        parent = db.find_batch_by_transfer_id(transfer_id)
        if parent:
            if parent["status"] == "rejected":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "HOPPAY_BATCH_REJECTED",
                        "message": f"Batch '{parent['id']}' was rejected and cannot be prepared.",
                    },
                )
            if parent["requires_approval"] and not parent["approved_at"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "HOPPAY_APPROVAL_REQUIRED",
                        "message": f"Batch '{parent['id']}' (${parent['total_usd']:,.2f}) requires human approval before preparing Solana transactions. Approve via POST /api/batches/{parent['id']}/approve first.",
                    },
                )
        try:
            prepared = await mh.prepare_transfer(transfer_id)
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "requote_available": exc.code in ("MH_040", "MH_034"),
                    "batch_id": parent["id"] if parent else None,
                },
            )
        tr_status = (prepared.get("transfer") or {}).get("status")
        if tr_status:
            db.sync_transfer_state(transfer_id, {"status": tr_status})
        return prepared

    @app.post("/api/transfers/{transfer_id}/confirm-broadcast", dependencies=[Depends(require_operator)])
    async def confirm_broadcast(transfer_id: int, req: ConfirmBroadcastRequest) -> dict[str, Any]:
        try:
            res = await mh.confirm_broadcast(
                transfer_id,
                route_init_signatures=req.route_init_signatures,
                keeper_funding_signature=req.keeper_funding_signature,
                orchestrator_init_signature=req.orchestrator_init_signature,
                session_init_signatures=req.session_init_signatures,
                dispersion_init_signatures=req.dispersion_init_signatures,
            )
        except MultiHopperAPIError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
            )
        tr_status = (res.get("transfer") or {}).get("status")
        if tr_status:
            db.sync_transfer_state(transfer_id, {"status": tr_status})
        return res

    @app.post("/api/demo/transfers/{transfer_id}/simulate-settle", dependencies=[Depends(require_operator)])
    async def demo_simulate_settle(transfer_id: int) -> dict[str, Any]:
        """Demo-only end-to-end protocol simulator (enforces approval gate + two-stage confirm + HMAC webhook)."""
        if not mh.is_demo:
            raise HTTPException(
                status_code=403,
                detail="Simulation endpoint is disabled in live devnet/mainnet mode.",
            )
        prepared = await prepare_transfer(transfer_id)
        ptx = prepared["preparedTxs"]
        steps: list[dict[str, Any]] = [{"stage": "prepare", "status": "ok"}]

        keeper_sig = f"demo_keeper_{transfer_id}"
        if ptx.get("keeperFundingTx"):
            c1 = await confirm_broadcast(
                transfer_id,
                ConfirmBroadcastRequest(route_init_signatures=[], keeper_funding_signature=keeper_sig),
            )
            steps.append({"stage": "keeper_funding_confirm", "result": c1["transfer"]})

        route_sigs = [f"demo_route_{transfer_id}_{i}" for i in range(len(ptx.get("routeInitTxs") or []))]
        orch_sig = f"demo_orch_{transfer_id}" if ptx.get("orchestratorInitTx") else None
        sess_sigs = [f"demo_sess_{transfer_id}_{i}" for i in range(len(ptx.get("sessionInitTxs") or []))]
        disp_sigs = [f"demo_disp_{transfer_id}_{i}" for i in range(len(ptx.get("dispersionInitTxs") or []))]

        c2 = await confirm_broadcast(
            transfer_id,
            ConfirmBroadcastRequest(
                route_init_signatures=route_sigs,
                keeper_funding_signature=keeper_sig,
                orchestrator_init_signature=orch_sig,
                session_init_signatures=sess_sigs or None,
                dispersion_init_signatures=disp_sigs or None,
            ),
        )
        steps.append({"stage": "full_bundle_confirm", "result": c2["transfer"]})

        settled = mh.demo_mark_settled(transfer_id)
        wh_body = json.dumps(
            {
                "event": "transfer.completed",
                "data": {
                    "transferId": transfer_id,
                    "phase": "settled",
                    "progress": settled["progress"],
                    "txSignature": (settled.get("signatures", {}).get("hops") or [""])[-1],
                },
            }
        ).encode("utf-8")
        sig = sign_webhook_payload(wh_body, wh_secret)
        assert verify_multihopper_signature(wh_body, sig, wh_secret)
        db.record_webhook_event("transfer.completed", str(transfer_id), json.loads(wh_body.decode("utf-8")))
        db.sync_transfer_state(
            transfer_id,
            {
                "status": settled["status"],
                "phase": settled["phase"],
                "progress": settled["progress"],
                "destinations": settled.get("destinations"),
                "route": settled.get("route"),
                "signatures": settled.get("signatures"),
            },
        )
        steps.append({"stage": "hmac_webhook_settled", "signature_prefix": sig[:16]})
        return {"transfer": settled, "steps": steps}

    @app.post("/webhooks/multihopper")
    async def receive_webhook(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        sig = request.headers.get("x-multihopper-signature", "")
        if not verify_multihopper_signature(raw_body, sig, wh_secret):
            raise HTTPException(status_code=401, detail="Invalid x-multihopper-signature")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        event_type = str(payload.get("event") or payload.get("type") or "unknown")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        transfer_id = str(data.get("transferId") or data.get("id") or "unknown")
        is_new = db.record_webhook_event(event_type, transfer_id, payload)
        return {
            "received": True,
            "duplicate": not is_new,
            "event": event_type,
            "transfer_id": transfer_id,
        }

    return app
