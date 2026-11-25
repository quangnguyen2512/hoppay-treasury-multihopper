"""MultiHopper v1 REST API client (Devnet/Mainnet live + exact on-chain fee schedule Demo simulator)."""

from __future__ import annotations

import base64
import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from hoppay.models import (
    SOL_MINT,
    CustomRoute,
    utc_now_iso,
    validate_custom_route,
)

# Official MultiHopper on-chain fee schedule (live since 2026-09-28; source: dev-docs.multihopper.com/concepts/pricing)
# Tuple of (tier_index, tier_name, max_usd_inclusive, flat_fee_lamports_per_hop)
OFFICIAL_FEE_TIERS: tuple[tuple[int, str, float, int], ...] = (
    (0, "tier_0", 50.00, 0),
    (1, "tier_1", 106.25, 125_000),
    (2, "tier_2", 162.50, 375_000),
    (3, "tier_3", 218.75, 625_000),
    (4, "tier_4", 275.00, 875_000),
    (5, "tier_5", 331.25, 1_125_000),
    (6, "tier_6", 387.50, 1_375_000),
    (7, "tier_7", 443.75, 1_625_000),
    (8, "tier_8", 500.00, 1_875_000),
    (9, "tier_9", float("inf"), 2_000_000),
)

PERCENT_FEE_BPS = 50  # 0.50% total; integrator receives 50% (25 bps = 0.25%) atomically on-chain
SCREENING_FEE_LAMPORTS_PER_WALLET = 2_000_000  # 0.002 SOL on mainnet compliance-enabled clusters

DEMO_HOP_LIBRARY_WALLETS: tuple[dict[str, Any], ...] = (
    {"address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "label": "Binance Hot Wallet 2", "category": "cex", "canEndRoute": True},
    {"address": "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "label": "Coinbase Hot Wallet 1", "category": "cex", "canEndRoute": True},
    {"address": "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "label": "Bybit Hot Wallet", "category": "cex", "canEndRoute": True},
    {"address": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "label": "Raydium Liquidity Pool V4", "category": "dex", "canEndRoute": False},
    {"address": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "label": "Jupiter Aggregator v6", "category": "dex", "canEndRoute": False},
    {"address": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "label": "Orca Whirlpool Router", "category": "dex", "canEndRoute": True},
    {"address": "DQTf1yf1YM4B48PqJyQ53SEWGDM6ib4YBV7P3wm2MCxj", "label": "Solana Ecosystem KOL #1", "category": "kol", "canEndRoute": True},
    {"address": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj", "label": "Solana Ecosystem KOL #2", "category": "kol", "canEndRoute": True},
    {"address": "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s", "label": "Metaplex Token Metadata", "category": "contracts", "canEndRoute": False},
    {"address": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "label": "SPL Memo Program", "category": "contracts", "canEndRoute": False},
    {"address": "GThUX1Atko4tqhN2NaiTazWSeFWMuiUiswQJy7cK4y8E", "label": "Active Treasury Router", "category": "other", "canEndRoute": True},
)

DEMO_BUNDLE_CATEGORY_MAP: dict[str, list[str]] = {
    "cex": ["cex"],
    "dex": ["dex"],
    "exchanges": ["cex", "dex"],
    "kol": ["kol"],
    "contracts": ["contracts"],
    "mix": ["cex", "dex", "famous", "kol", "contracts", "other"],
}


class MultiHopperAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"[{status_code} {code}] {message}")


def compute_official_estimate(
    *,
    token_mint: str,
    amount_raw: int,
    token_decimals: int,
    hops: int,
    token_price_usd: float,
    destination_count: int | None = None,
    arrival_seconds: int | None = None,
    is_mainnet: bool = False,
    is_test_mode: bool = True,
    route_meta: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Reproduce MultiHopper's exact tier & fee formula from concepts/pricing."""
    is_sol = token_mint == SOL_MINT
    gross_tokens = amount_raw / (10**token_decimals)
    usd_equiv = gross_tokens * token_price_usd

    # Find matching tier using post-fee hop value
    chosen = OFFICIAL_FEE_TIERS[-1]
    hop_amount_raw = 0
    percent_fee_raw = 0
    total_flat_lamports = 0
    for tier_idx, tier_name, max_usd, flat_per_hop in OFFICIAL_FEE_TIERS:
        flat_total = flat_per_hop * hops
        available = amount_raw - flat_total if is_sol else amount_raw
        if available <= 0:
            continue
        # hop + floor(hop * 50 / 10000) <= available  =>  hop ≈ floor(available * 10000 / 10050)
        hop_candidate = (available * 10_000) // (10_000 + PERCENT_FEE_BPS)
        pct_fee = (hop_candidate * PERCENT_FEE_BPS) // 10_000
        hop_usd = (hop_candidate / (10**token_decimals)) * token_price_usd
        if hop_usd <= max_usd or math.isinf(max_usd):
            chosen = (tier_idx, tier_name, max_usd, flat_per_hop)
            hop_amount_raw = hop_candidate
            percent_fee_raw = pct_fee
            total_flat_lamports = flat_total
            break

    tier_idx, tier_name, _, flat_per_hop = chosen
    integrator_share = percent_fee_raw // 2
    usd_value_micros = int(round(((hop_amount_raw / (10**token_decimals)) * token_price_usd) * 1_000_000))

    dest_n = destination_count or 0
    dispersion_rent = dest_n * 2_400_000 if dest_n else 0
    screened_wallets = 1 + (dest_n if dest_n else 1)
    if route_meta:
        # Custom routes screen every hop wallet at deploy
        screened_wallets = 1 + route_meta["hopCount"] + dest_n
    if arrival_seconds and arrival_seconds > 6 * 3600:
        screened_wallets *= 2
    screening_fee = screened_wallets * SCREENING_FEE_LAMPORTS_PER_WALLET if is_mainnet else 0

    route_init_rent = 15_407_640
    orch_rent = 13_655_040
    ata_rent = 2_428_240
    tx_fees = (4 + hops + dest_n) * 5_000
    refundable = 16_083_280 + dispersion_rent
    upfront_sol = (
        (amount_raw if is_sol else 0)
        + route_init_rent
        + orch_rent
        + ata_rent
        + tx_fees
        + (0 if is_sol else total_flat_lamports)
        + screening_fee
        + dispersion_rent
    )

    out: dict[str, Any] = {
        "tier": tier_name,
        "tierIndex": tier_idx,
        "percentFeeBps": PERCENT_FEE_BPS,
        "percentFeeRaw": str(percent_fee_raw),
        "flatFeeLamportsPerHop": flat_per_hop,
        "flatFeeLamportsPerRoute": 0,
        "totalFlatFeeLamports": total_flat_lamports,
        "integratorPercentShare": str(integrator_share),
        "integratorFlatShare": 0,
        "usdEquivalent": round(usd_equiv, 2),
        "usdValueMicros": str(usd_value_micros),
        "priceSource": "override" if is_test_mode else "onchain_pool",
        "hopAmountRaw": str(hop_amount_raw),
        "cappedAtTierBoundary": False,
        "destinationCount": destination_count,
        "isTestMode": is_test_mode,
        "tokens": {
            "amountRaw": str(amount_raw),
            "feeRaw": str(percent_fee_raw),
            "netDeliveredRaw": str(hop_amount_raw),
            "decimals": token_decimals,
        },
        "sol": {
            "requiredSolUpFrontLamports": upfront_sol,
            "minTransferAmountLamports": 2_039_280,
            "breakdown": {
                "routeInitRentLamports": route_init_rent,
                "orchestratorRentLamports": orch_rent,
                "ataRentLamports": ata_rent,
                "transactionFeesLamports": tx_fees,
                "priorityFeesLamports": 0,
                "protocolFlatFeeLamports": total_flat_lamports,
                "screeningFeeLamports": screening_fee,
                "dispersionRentLamports": dispersion_rent,
            },
            "refundableLamports": refundable,
            "netCostLamports": max(0, upfront_sol - refundable),
        },
    }
    if route_meta:
        out["route"] = route_meta
    return out


class MultiHopperClient:
    """Async REST client for official MultiHopper v1 API (devnet & mainnet + deterministic demo)."""

    def __init__(
        self,
        *,
        env: str = "demo",
        api_key: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.env = env.lower().strip()
        self.api_key = api_key.strip()
        self.transport = transport
        self.timeout = timeout
        self._demo_seq = 100
        self._demo_tpl_seq = 0
        self._demo_transfers: dict[int, dict[str, Any]] = {}
        self._demo_by_ext: dict[str, int] = {}
        self._demo_templates: dict[int, dict[str, Any]] = {}
        self._seed_demo_templates()

    def hydrate_demo_transfers(self, batches: list[dict[str, Any]]) -> None:
        """Reload persisted transfers from SQLite into Demo simulator memory across server restarts."""
        if not self.is_demo:
            return
        max_id = self._demo_seq
        for batch in batches:
            for tr in batch.get("transfers") or []:
                tid = tr.get("id")
                if isinstance(tid, int):
                    self._demo_transfers[tid] = dict(tr)
                    ext = tr.get("externalId")
                    if ext:
                        self._demo_by_ext[str(ext)] = tid
                    if tid > max_id:
                        max_id = tid
        self._demo_seq = max_id

    def _seed_demo_templates(self) -> None:
        now = utc_now_iso()
        presets = [
            (
                "dao-payroll-3hop",
                "Standard 3-hop exchange + KOL treasury route (10m total arrival)",
                {
                    "hops": [
                        {"bundle": "cex", "waitSeconds": 120},
                        {"bundle": "dex", "waitSeconds": 180},
                        {"bundle": "kol", "waitSeconds": 300},
                    ]
                },
            ),
            (
                "stealth-4hop-scheduled",
                "4-hop mixed-category delayed disbursement (20m total arrival)",
                {
                    "hops": [
                        {"bundle": "cex", "waitSeconds": 180},
                        {"bundle": "contracts", "waitSeconds": 240},
                        {"bundle": "exchanges", "waitSeconds": 360},
                        {"bundle": "mix", "waitSeconds": 420},
                    ]
                },
            ),
        ]
        for name, desc, route_dict in presets:
            self._demo_tpl_seq += 1
            tid = self._demo_tpl_seq
            tpl = self._build_demo_template(tid, name, desc, route_dict, created_at=now, updated_at=now)
            self._demo_templates[tid] = tpl

    @staticmethod
    def _build_demo_template(
        tid: int,
        name: str,
        description: str | None,
        route_dict: dict[str, Any],
        *,
        created_at: str,
        updated_at: str,
    ) -> dict[str, Any]:
        parsed = CustomRoute.model_validate(route_dict)
        single_probs, arrival = validate_custom_route(parsed, for_destinations=False)
        multi_probs, _ = validate_custom_route(parsed, for_destinations=True)
        problems: dict[str, list[str]] = {}
        if single_probs:
            problems["recipientWallet"] = single_probs
        if multi_probs:
            problems["destinations"] = multi_probs
        return {
            "id": tid,
            "name": name,
            "description": description,
            "route": parsed.to_api_dict(),
            "arrivalSeconds": arrival,
            "usableFor": {
                "recipientWallet": len(single_probs) == 0,
                "destinations": len(multi_probs) == 0,
            },
            "problems": problems,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }

    @property
    def base_url(self) -> str:
        if self.env == "mainnet":
            return "https://multihopper.com/api/v1"
        return "https://devnet.multihopper.com/api/v1"

    @property
    def is_demo(self) -> bool:
        return self.env == "demo" or not self.api_key

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "HopPayTreasury/0.2 (MultiHopper Build & Route)",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if method in ("POST", "PATCH", "DELETE") and not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            resp = await client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._headers(idempotency_key),
            )
            if resp.status_code >= 400:
                code = "HTTP_ERROR"
                msg = resp.text[:300]
                try:
                    err = resp.json().get("error") or {}
                    code = str(err.get("code") or code)
                    msg = str(err.get("message") or msg)
                except Exception:
                    pass
                raise MultiHopperAPIError(
                    resp.status_code,
                    code,
                    msg,
                    retry_after=resp.headers.get("retry-after"),
                )
            if resp.status_code == 204 or not resp.content:
                return {"deleted": True}
            return resp.json()

    def _resolve_demo_custom_route(
        self,
        *,
        route: dict[str, Any] | None,
        route_template_id: int | None,
        for_destinations: bool,
        source_owner: str | None = None,
        recipient_wallet: str | None = None,
        destination_wallets: list[str] | None = None,
    ) -> tuple[CustomRoute | None, int | None, int | None]:
        """Validate and resolve a custom route or routeTemplateId in Demo mode."""
        raw_route = route
        if route_template_id is not None:
            tpl = self._demo_templates.get(route_template_id)
            if not tpl:
                raise MultiHopperAPIError(
                    404, "MH_021", f"Route template {route_template_id} not found in integration"
                )
            raw_route = tpl["route"]
        if raw_route is None:
            return None, None, None

        parsed = CustomRoute.model_validate(raw_route)
        try:
            problems, arrival_sec = validate_custom_route(
                parsed,
                for_destinations=for_destinations,
                source_owner=source_owner,
                recipient_wallet=recipient_wallet,
                destination_wallets=destination_wallets,
                is_devnet=(self.env == "devnet"),
            )
        except ValueError as exc:
            raise MultiHopperAPIError(400, "MH_019", str(exc)) from exc
        if problems:
            raise MultiHopperAPIError(400, "MH_018", "Invalid custom route — " + "; ".join(problems))

        chain_hops = len(parsed.hops) if for_destinations else len(parsed.hops) + 1
        return parsed, chain_hops, arrival_sec

    def _draw_demo_route_hops(
        self,
        parsed: CustomRoute,
        *,
        for_destinations: bool,
        source_owner: str,
        recipient_wallet: str | None,
        destination_wallets: list[str],
    ) -> list[dict[str, Any]]:
        used: set[str] = {source_owner, *destination_wallets}
        if recipient_wallet:
            used.add(recipient_wallet)
        for h in parsed.hops:
            if h.wallet:
                used.add(h.wallet)

        now_dt = datetime.now(timezone.utc)
        elapsed = 0
        resolved: list[dict[str, Any]] = []
        total_n = len(parsed.hops)
        for idx, h in enumerate(parsed.hops):
            scheduled = (now_dt + timedelta(seconds=elapsed)).isoformat()
            if h.wallet:
                resolved.append(
                    {
                        "index": idx,
                        "address": h.wallet,
                        "label": None,
                        "category": None,
                        "scheduledAt": scheduled,
                        "executedAt": None,
                        "txSignature": None,
                    }
                )
            else:
                bundle = h.bundle or "mix"
                allowed_cats = set(DEMO_BUNDLE_CATEGORY_MAP.get(bundle, []))
                need_end = for_destinations and idx == total_n - 1
                candidates = [
                    w
                    for w in DEMO_HOP_LIBRARY_WALLETS
                    if w["category"] in allowed_cats
                    and w["address"] not in used
                    and (not need_end or w["canEndRoute"])
                ]
                if not candidates:
                    raise MultiHopperAPIError(
                        400,
                        "MH_020",
                        f"Hop library bundle '{bundle}' has no unused wallet remaining for route.hops[{idx}]",
                    )
                pick = candidates[idx % len(candidates)]
                used.add(pick["address"])
                resolved.append(
                    {
                        "index": idx,
                        "address": pick["address"],
                        "label": pick["label"],
                        "category": pick["category"],
                        "scheduledAt": scheduled,
                        "executedAt": None,
                        "txSignature": None,
                    }
                )
            elapsed += h.wait_seconds

        if not for_destinations and recipient_wallet:
            resolved.append(
                {
                    "index": len(resolved),
                    "address": recipient_wallet,
                    "label": None,
                    "category": None,
                    "scheduledAt": (now_dt + timedelta(seconds=elapsed)).isoformat(),
                    "executedAt": None,
                    "txSignature": None,
                }
            )
        return resolved

    async def estimate(
        self,
        *,
        token_mint: str,
        amount_raw: str,
        token_decimals: int = 9,
        hops: int | None = 3,
        destination_count: int | None = None,
        arrival_seconds: int | None = 600,
        route: dict[str, Any] | None = None,
        route_template_id: int | None = None,
        token_price_usd: float | None = 150.0,
    ) -> dict[str, Any]:
        if self.is_demo:
            for_dest = bool(destination_count and destination_count > 1)
            parsed_route, chain_hops, custom_arrival = self._resolve_demo_custom_route(
                route=route,
                route_template_id=route_template_id,
                for_destinations=for_dest,
            )
            eff_hops = chain_hops if chain_hops is not None else (hops or 3)
            eff_arrival = custom_arrival if custom_arrival is not None else arrival_seconds
            route_meta = (
                {"hopCount": eff_hops, "arrivalSeconds": eff_arrival or 0}
                if parsed_route is not None
                else None
            )
            return compute_official_estimate(
                token_mint=token_mint,
                amount_raw=int(amount_raw),
                token_decimals=token_decimals,
                hops=eff_hops,
                token_price_usd=token_price_usd or 1.0,
                destination_count=destination_count,
                arrival_seconds=eff_arrival,
                is_mainnet=(self.env == "mainnet"),
                is_test_mode=True,
                route_meta=route_meta,
            )

        payload: dict[str, Any] = {
            "tokenMint": token_mint,
            "amountRaw": str(amount_raw),
            "tokenDecimals": token_decimals,
        }
        if route is not None:
            payload["route"] = route
        elif route_template_id is not None:
            payload["routeTemplateId"] = route_template_id
        else:
            if hops is not None:
                payload["hops"] = hops
            if arrival_seconds is not None:
                payload["arrivalSeconds"] = arrival_seconds
        if destination_count is not None:
            payload["destinationCount"] = destination_count
        if token_price_usd is not None:
            payload["tokenPriceUsd"] = token_price_usd
        return await self._request("POST", "/transfers/estimate", json_body=payload)

    async def create_transfer(
        self,
        *,
        token_mint: str,
        amount_raw: str,
        amount_tokens: str,
        token_decimals: int,
        token_symbol: str,
        source_owner: str,
        hops: int = 3,
        arrival_seconds: int = 600,
        recipient_wallet: str | None = None,
        destinations: list[dict[str, str]] | None = None,
        route: dict[str, Any] | None = None,
        route_template_id: int | None = None,
        external_id: str | None = None,
        token_price_usd: float | None = None,
    ) -> dict[str, Any]:
        if bool(recipient_wallet) == bool(destinations):
            raise ValueError("Provide exactly one of recipient_wallet or destinations")

        if self.is_demo:
            if external_id and external_id in self._demo_by_ext:
                return self._demo_transfers[self._demo_by_ext[external_id]]

            for_dest = bool(destinations)
            dest_wallets = [d["wallet"] for d in (destinations or [])]
            parsed_route, chain_hops, custom_arrival = self._resolve_demo_custom_route(
                route=route,
                route_template_id=route_template_id,
                for_destinations=for_dest,
                source_owner=source_owner,
                recipient_wallet=recipient_wallet,
                destination_wallets=dest_wallets,
            )
            eff_hops = chain_hops if chain_hops is not None else hops
            eff_arrival = custom_arrival if custom_arrival is not None else arrival_seconds
            resolved_route = None
            if parsed_route is not None:
                resolved_route = {
                    "hops": self._draw_demo_route_hops(
                        parsed_route,
                        for_destinations=for_dest,
                        source_owner=source_owner,
                        recipient_wallet=recipient_wallet,
                        destination_wallets=dest_wallets,
                    )
                }

            est = compute_official_estimate(
                token_mint=token_mint,
                amount_raw=int(amount_raw),
                token_decimals=token_decimals,
                hops=eff_hops,
                token_price_usd=token_price_usd or 150.0,
                destination_count=len(destinations) if destinations else None,
                arrival_seconds=eff_arrival,
                is_mainnet=False,
                is_test_mode=True,
                route_meta={"hopCount": eff_hops, "arrivalSeconds": eff_arrival} if parsed_route else None,
            )
            self._demo_seq += 1
            tid = self._demo_seq
            hop_total = int(est["hopAmountRaw"])
            gross_total = int(amount_raw)
            dest_out = None
            if destinations:
                allocated = 0
                dest_out = []
                for idx, d in enumerate(destinations):
                    share = int(d["amountRaw"])
                    if idx == len(destinations) - 1:
                        net_share = hop_total - allocated
                    else:
                        net_share = (hop_total * share) // gross_total
                        allocated += net_share
                    dest_out.append(
                        {
                            "wallet": d["wallet"],
                            "amountRaw": str(net_share),
                            "status": "pending",
                            "payoutSignature": None,
                            "paidAt": None,
                        }
                    )
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            expires = (now_dt + timedelta(minutes=30)).isoformat()
            transfer: dict[str, Any] = {
                "id": tid,
                "externalId": external_id,
                "supportBundleId": f"MH-hoppay-{tid}",
                "tokenMint": token_mint,
                "amountRaw": str(amount_raw),
                "amountTokens": amount_tokens,
                "tokenDecimals": token_decimals,
                "tokenSymbol": token_symbol,
                "sourceOwner": source_owner,
                "recipientWallet": recipient_wallet,
                "destinationCount": len(destinations) if destinations else None,
                "destinations": dest_out,
                "recipientReceivesRaw": str(hop_total),
                "fundingStrategy": "direct_orchestration",
                "hops": eff_hops,
                "arrivalSeconds": eff_arrival,
                "pricingTier": est["tier"],
                "percentFeeBps": est["percentFeeBps"],
                "percentFeeRaw": est["percentFeeRaw"],
                "integratorPercentShare": est["integratorPercentShare"],
                "flatFeeLamportsPerHop": est["flatFeeLamportsPerHop"],
                "totalFlatFeeLamports": est["totalFlatFeeLamports"],
                "status": "quote",
                "phase": "quoted",
                "progress": {"hopsCompleted": 0, "hopsTotal": eff_hops},
                "signatures": {"routeInit": [], "sessionInit": [], "hops": []},
                "recovery": None,
                "lastError": None,
                "isTest": True,
                "quotedAt": now,
                "expiresAt": expires,
                "createdAt": now,
            }
            if resolved_route is not None:
                transfer["route"] = resolved_route
            self._demo_transfers[tid] = transfer
            if external_id:
                self._demo_by_ext[external_id] = tid
            return transfer

        payload: dict[str, Any] = {
            "tokenMint": token_mint,
            "amountRaw": str(amount_raw),
            "amountTokens": amount_tokens,
            "tokenDecimals": token_decimals,
            "tokenSymbol": token_symbol,
            "sourceOwner": source_owner,
        }
        if route is not None:
            payload["route"] = route
        elif route_template_id is not None:
            payload["routeTemplateId"] = route_template_id
        else:
            payload["hops"] = hops
            payload["arrivalSeconds"] = arrival_seconds
        if recipient_wallet:
            payload["recipientWallet"] = recipient_wallet
        if destinations:
            payload["destinations"] = destinations
        if external_id:
            payload["externalId"] = external_id
        if token_price_usd:
            payload["tokenPriceUsd"] = token_price_usd
        return await self._request(
            "POST",
            "/transfers",
            json_body=payload,
            idempotency_key=external_id or str(uuid.uuid4()),
        )

    async def get_transfer(self, transfer_id: int) -> dict[str, Any]:
        if self.is_demo:
            tr = self._demo_transfers.get(transfer_id)
            if not tr:
                raise MultiHopperAPIError(404, "MH_030", f"Transfer {transfer_id} not found")
            return tr
        return await self._request("GET", f"/transfers/{transfer_id}")

    async def get_transfer_by_external_id(self, external_id: str) -> dict[str, Any]:
        if self.is_demo:
            tid = self._demo_by_ext.get(external_id)
            if tid is None:
                raise MultiHopperAPIError(404, "MH_030", f"Transfer for externalId '{external_id}' not found")
            return self._demo_transfers[tid]
        return await self._request("GET", f"/transfers/by-external/{external_id}")

    async def list_transfers(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if self.is_demo:
            items = list(self._demo_transfers.values())
            if status:
                items = [t for t in items if t.get("status") == status]
            items = sorted(items, key=lambda x: x["id"], reverse=True)
            sliced = items[offset : offset + limit]
            return {
                "items": sliced,
                "pagination": {"total": len(items), "limit": limit, "offset": offset},
            }
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._request("GET", "/transfers", params=params)

    async def prepare_transfer(self, transfer_id: int) -> dict[str, Any]:
        if self.is_demo:
            tr = self._demo_transfers.get(transfer_id)
            if not tr:
                raise MultiHopperAPIError(404, "MH_030", f"Transfer {transfer_id} not found")
            if tr["status"] in ("completed", "refunded", "expired"):
                raise MultiHopperAPIError(
                    409,
                    "MH_035",
                    f"Transfer not in a state that allows this operation — Transfer is {tr['status']}; nothing left to prepare",
                )
            keeper_done = tr["status"] in ("funding_confirmed", "processing", "active")
            if not keeper_done:
                tr["status"] = "awaiting_signature"
                tr["phase"] = "deploying"
            dest_n = tr.get("destinationCount") or 0
            fake_tx = lambda tag: base64.b64encode(f"UNSIGNED_DEMO_TX:{transfer_id}:{tag}".encode()).decode()
            return {
                "transfer": {"id": transfer_id, "status": tr["status"]},
                "preparedTxs": {
                    "keeperFundingTx": None if keeper_done else fake_tx("keeperFunding"),
                    "routeInitTxs": [{"base64": fake_tx("routeInit_0")}],
                    "orchestratorInitTx": fake_tx("orchestratorInit"),
                    "sessionInitTxs": [fake_tx(f"session_{i}") for i in range(tr["hops"])],
                    "dispersionInitTxs": [fake_tx(f"dispersion_{i}") for i in range(dest_n + 1)] if dest_n else [],
                    "recentBlockhash": hashlib.sha256(f"bh:{transfer_id}".encode()).hexdigest()[:44],
                    "lastValidBlockHeight": 299_000_000,
                    "resume": {
                        "routeAlreadyDeployed": False,
                        "existingHopCount": 0,
                        "totalHops": tr["hops"],
                        "orchestratorAlreadyInitialized": False,
                        "completedStepIndices": [],
                        "totalSteps": tr["hops"],
                        "keeperAlreadyFunded": keeper_done,
                        "dispersion": (
                            {
                                "totalDestinations": dest_n,
                                "orchestratorInitialized": False,
                                "stepsInitialized": 0,
                                "routeExitBound": False,
                            }
                            if dest_n
                            else None
                        ),
                        "nothingToDo": False,
                    },
                },
            }
        return await self._request(
            "POST",
            f"/transfers/{transfer_id}/prepare",
            idempotency_key=str(uuid.uuid4()),
        )

    async def confirm_broadcast(
        self,
        transfer_id: int,
        *,
        route_init_signatures: list[str],
        keeper_funding_signature: str | None = None,
        orchestrator_init_signature: str | None = None,
        session_init_signatures: list[str] | None = None,
        dispersion_init_signatures: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.is_demo:
            tr = self._demo_transfers.get(transfer_id)
            if not tr:
                raise MultiHopperAPIError(404, "MH_030", f"Transfer {transfer_id} not found")
            if not route_init_signatures:
                if not keeper_funding_signature:
                    raise MultiHopperAPIError(400, "MH_039", "keeperFundingSignature is required")
                tr["status"] = "funding_confirmed"
                tr["phase"] = "deploying"
            else:
                if tr.get("destinationCount") and not dispersion_init_signatures:
                    raise MultiHopperAPIError(
                        409,
                        "MH_041",
                        "Destinations not yet bound on chain: broadcast preparedTxs.dispersionInitTxs, then confirm again",
                    )
                tr["status"] = "processing"
                tr["phase"] = "executing"
                tr["signatures"] = {
                    "routeInit": list(route_init_signatures),
                    "sessionInit": list(session_init_signatures or []),
                    "hops": [],
                }
            return {"transfer": {"id": transfer_id, "status": tr["status"]}}

        payload: dict[str, Any] = {"routeInitSignatures": route_init_signatures}
        if keeper_funding_signature:
            payload["keeperFundingSignature"] = keeper_funding_signature
        if orchestrator_init_signature:
            payload["orchestratorInitSignature"] = orchestrator_init_signature
        if session_init_signatures:
            payload["sessionInitSignatures"] = session_init_signatures
        if dispersion_init_signatures:
            payload["dispersionInitSignatures"] = dispersion_init_signatures
        return await self._request(
            "POST",
            f"/transfers/{transfer_id}/confirm-broadcast",
            json_body=payload,
            idempotency_key=str(uuid.uuid4()),
        )

    def demo_mark_settled(self, transfer_id: int) -> dict[str, Any]:
        """Advance a demo transfer through all hops and destination payouts (demo mode only)."""
        tr = self._demo_transfers.get(transfer_id)
        if not tr:
            raise MultiHopperAPIError(404, "MH_030", f"Transfer {transfer_id} not found")
        now = utc_now_iso()
        hops_n = int(tr.get("hops") or 3)
        hop_sigs = [
            hashlib.sha256(f"hop:{transfer_id}:{i}".encode()).hexdigest()[:44] for i in range(hops_n)
        ]
        tr["status"] = "completed"
        tr["phase"] = "settled"
        tr["progress"] = {"hopsCompleted": hops_n, "hopsTotal": hops_n}
        sigs = tr.get("signatures") or {"routeInit": [], "sessionInit": [], "hops": []}
        sigs["hops"] = hop_sigs
        tr["signatures"] = sigs
        if isinstance(tr.get("route"), dict) and isinstance(tr["route"].get("hops"), list):
            for idx, h in enumerate(tr["route"]["hops"]):
                h["executedAt"] = now
                h["txSignature"] = hop_sigs[idx % len(hop_sigs)]
        if isinstance(tr.get("destinations"), list):
            for idx, d in enumerate(tr["destinations"]):
                d["status"] = "paid"
                d["paidAt"] = now
                d["payoutSignature"] = hashlib.sha256(
                    f"payout:{transfer_id}:{d['wallet']}:{idx}".encode()
                ).hexdigest()[:44]
        return tr

    async def get_hop_library(
        self,
        *,
        bundle: str | None = None,
        category: str | None = None,
        can_end_route: bool | None = None,
    ) -> dict[str, Any]:
        if self.is_demo:
            if self.env == "devnet":
                raise MultiHopperAPIError(
                    400, "MH_019", "Hop library is not served on devnet; use explicit wallet addresses"
                )
            wallets = list(DEMO_HOP_LIBRARY_WALLETS)
            if bundle:
                cats = set(DEMO_BUNDLE_CATEGORY_MAP.get(bundle.lower(), []))
                wallets = [w for w in wallets if w["category"] in cats]
            if category:
                wallets = [w for w in wallets if w["category"] == category.lower()]
            if can_end_route is not None:
                wallets = [w for w in wallets if w["canEndRoute"] is can_end_route]
            bundles = [
                {
                    "id": bid,
                    "categories": cats,
                    "available": sum(1 for w in DEMO_HOP_LIBRARY_WALLETS if w["category"] in cats),
                }
                for bid, cats in DEMO_BUNDLE_CATEGORY_MAP.items()
            ]
            return {
                "wallets": wallets,
                "bundles": bundles,
                "categories": ["cex", "dex", "famous", "kol", "contracts", "other"],
            }
        params: dict[str, Any] = {}
        if bundle:
            params["bundle"] = bundle
        if category:
            params["category"] = category
        if can_end_route is not None:
            params["canEndRoute"] = "true" if can_end_route else "false"
        return await self._request("GET", "/hop-library", params=params)

    async def list_route_templates(self, name: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        if self.is_demo:
            items = sorted(self._demo_templates.values(), key=lambda x: x["name"])
            if name:
                items = [t for t in items if t["name"] == name]
            return items
        params = {"name": name} if name else None
        return await self._request("GET", "/route-templates", params=params)

    async def create_route_template(
        self,
        *,
        name: str,
        route: dict[str, Any],
        description: str | None = None,
    ) -> dict[str, Any]:
        if self.is_demo:
            if len(self._demo_templates) >= 100:
                raise MultiHopperAPIError(400, "MH_023", "Route template limit reached (100 per integration)")
            if any(t["name"] == name for t in self._demo_templates.values()):
                raise MultiHopperAPIError(409, "MH_022", f"Route template named '{name}' already exists")
            now = utc_now_iso()
            tpl = self._build_demo_template(
                self._demo_tpl_seq + 1, name, description, route, created_at=now, updated_at=now
            )
            if not tpl["usableFor"]["recipientWallet"] and not tpl["usableFor"]["destinations"]:
                raise MultiHopperAPIError(400, "MH_018", f"Invalid custom route: {tpl['problems']}")
            self._demo_tpl_seq += 1
            self._demo_templates[self._demo_tpl_seq] = tpl
            return tpl
        payload: dict[str, Any] = {"name": name, "route": route}
        if description is not None:
            payload["description"] = description
        return await self._request("POST", "/route-templates", json_body=payload)

    async def get_route_template(self, template_id: int) -> dict[str, Any]:
        if self.is_demo:
            tpl = self._demo_templates.get(template_id)
            if not tpl:
                raise MultiHopperAPIError(404, "MH_021", f"Route template {template_id} not found")
            return tpl
        return await self._request("GET", f"/route-templates/{template_id}")

    async def update_route_template(
        self,
        template_id: int,
        *,
        name: str | None = None,
        route: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if self.is_demo:
            existing = self._demo_templates.get(template_id)
            if not existing:
                raise MultiHopperAPIError(404, "MH_021", f"Route template {template_id} not found")
            new_name = name if name is not None else existing["name"]
            if new_name != existing["name"] and any(
                t["name"] == new_name for tid, t in self._demo_templates.items() if tid != template_id
            ):
                raise MultiHopperAPIError(409, "MH_022", f"Route template named '{new_name}' already exists")
            new_desc = description if description is not None else existing.get("description")
            new_route = route if route is not None else existing["route"]
            now = utc_now_iso()
            updated = self._build_demo_template(
                template_id,
                new_name,
                new_desc,
                new_route,
                created_at=existing["createdAt"],
                updated_at=now,
            )
            if not updated["usableFor"]["recipientWallet"] and not updated["usableFor"]["destinations"]:
                raise MultiHopperAPIError(400, "MH_018", f"Invalid custom route: {updated['problems']}")
            self._demo_templates[template_id] = updated
            return updated
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if route is not None:
            payload["route"] = route
        return await self._request("PATCH", f"/route-templates/{template_id}", json_body=payload)

    async def delete_route_template(self, template_id: int) -> dict[str, Any]:
        if self.is_demo:
            if template_id not in self._demo_templates:
                raise MultiHopperAPIError(404, "MH_021", f"Route template {template_id} not found")
            del self._demo_templates[template_id]
            return {"deleted": True, "id": template_id}
        return await self._request("DELETE", f"/route-templates/{template_id}")

    async def list_webhooks(self) -> dict[str, Any] | list[dict[str, Any]]:
        if self.is_demo:
            return {"items": [], "isTestMode": True}
        return await self._request("GET", "/webhooks")

    async def register_webhook(self, url: str, events: list[str] | None = None) -> dict[str, Any]:
        if self.is_demo:
            return {
                "id": "wh_demo_1",
                "url": url,
                "events": events or ["transfer.completed", "transfer.failed", "payout.completed"],
                "secret": "whsec_demo_generated_local",
            }
        payload: dict[str, Any] = {"url": url}
        if events:
            payload["events"] = events
        return await self._request("POST", "/webhooks", json_body=payload)

    async def delete_webhook(self, webhook_id: str | int) -> dict[str, Any]:
        if self.is_demo:
            return {"deleted": True, "id": str(webhook_id)}
        return await self._request("DELETE", f"/webhooks/{webhook_id}")

    async def get_usage(self, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        if self.is_demo:
            transfers = list(self._demo_transfers.values())
            completed = [t for t in transfers if t.get("status") == "completed"]
            total_vol = sum(int(t["amountRaw"]) for t in transfers)
            sol_fees = sum(
                int(t.get("integratorPercentShare") or 0)
                for t in completed
                if t.get("tokenMint") == SOL_MINT
            )
            spl_fees_by_mint: dict[tuple[str, int], int] = {}
            for t in completed:
                mint = str(t.get("tokenMint") or "")
                if mint and mint != SOL_MINT:
                    key = (mint, int(t.get("tokenDecimals") or 6))
                    spl_fees_by_mint[key] = spl_fees_by_mint.get(key, 0) + int(
                        t.get("integratorPercentShare") or 0
                    )
            token_fees_earned = [
                {"tokenMint": mint, "tokenDecimals": dec, "amountRaw": str(amt)}
                for (mint, dec), amt in sorted(spl_fees_by_mint.items())
                if amt > 0
            ]
            return {
                "totalTransfers": len(transfers),
                "completedTransfers": len(completed),
                "failedTransfers": sum(1 for t in transfers if t.get("status") == "failed"),
                "totalVolumeRaw": str(total_vol),
                "totalFlatFeeEarned": sol_fees,
                "totalPayouts": len(completed),
                "tokenFeesEarned": token_fees_earned,
                "isTestMode": True,
            }
        params: dict[str, Any] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        return await self._request("GET", "/usage", params=params)
