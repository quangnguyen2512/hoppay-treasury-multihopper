"""Domain models, MultiHopper v1 schemas, custom route validation, CSV chunking, and treasury policy guards."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_MINT_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
SOL_RENT_EXEMPT_MIN_LAMPORTS = 890_880
MAX_DESTINATIONS_PER_ROUTE = 20
MIN_HOP_WAIT_SECONDS = 20
MAX_ROUTE_WAIT_SECONDS = 2_592_000  # 30 days

VALID_HOP_BUNDLES: frozenset[str] = frozenset(
    {"cex", "dex", "exchanges", "kol", "contracts", "mix"}
)
PROTOCOL_RESERVED_ADDRESSES: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "SysvarRent111111111111111111111111111111111",
        "SysvarC1ock11111111111111111111111111111111",
    }
)

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._ -]{0,63}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_wallet(addr: str) -> bool:
    return bool(BASE58_RE.match((addr or "").strip()))


def tokens_to_raw(amount_tokens: str | float | Decimal, decimals: int) -> int:
    try:
        dec = Decimal(str(amount_tokens).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid token amount: {amount_tokens}") from exc
    if dec <= 0:
        raise ValueError("Token amount must be positive")
    scale = Decimal(10) ** decimals
    return int((dec * scale).to_integral_value(rounding=ROUND_DOWN))


def raw_to_tokens(amount_raw: int | str, decimals: int) -> str:
    scale = Decimal(10) ** decimals
    val = (Decimal(str(amount_raw)) / scale).normalize()
    return format(val, "f")


class CustomHop(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    wallet: str | None = Field(default=None)
    bundle: str | None = Field(default=None)
    wait_seconds: int = Field(
        ...,
        alias="waitSeconds",
        ge=MIN_HOP_WAIT_SECONDS,
        le=MAX_ROUTE_WAIT_SECONDS,
    )

    @model_validator(mode="after")
    def check_exactly_one_target(self) -> "CustomHop":
        has_wallet = bool(self.wallet and self.wallet.strip())
        has_bundle = bool(self.bundle and self.bundle.strip())
        if has_wallet == has_bundle:
            raise ValueError("Each custom hop must specify exactly one of 'wallet' or 'bundle'")
        if has_wallet:
            self.wallet = self.wallet.strip()
            if not validate_wallet(self.wallet):
                raise ValueError(f"Invalid Solana wallet in custom hop: '{self.wallet}'")
        if has_bundle:
            self.bundle = self.bundle.strip().lower()
            if self.bundle not in VALID_HOP_BUNDLES:
                raise ValueError(
                    f"Unknown hop library bundle '{self.bundle}'. Allowed: {sorted(VALID_HOP_BUNDLES)}"
                )
        return self

    def to_api_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"waitSeconds": self.wait_seconds}
        if self.wallet:
            out["wallet"] = self.wallet
        else:
            out["bundle"] = self.bundle
        return out


class CustomRoute(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    hops: list[CustomHop] = Field(..., min_length=2, max_length=10)

    def to_api_dict(self) -> dict[str, Any]:
        return {"hops": [h.to_api_dict() for h in self.hops]}

    @property
    def total_wait_seconds(self) -> int:
        return sum(h.wait_seconds for h in self.hops)


class RouteTemplateCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    route: CustomRoute

    @model_validator(mode="after")
    def validate_name_format(self) -> "RouteTemplateCreate":
        self.name = self.name.strip()
        if not TEMPLATE_NAME_RE.match(self.name):
            raise ValueError(
                "Template name must be 1-64 chars (letters, digits, spaces, '.', '_', '-') starting with a letter or digit"
            )
        return self


class RouteTemplateUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    route: CustomRoute | None = Field(default=None)

    @model_validator(mode="after")
    def validate_update_fields(self) -> "RouteTemplateUpdate":
        if self.name is None and self.description is None and self.route is None:
            raise ValueError("Provide at least one of 'name', 'description', or 'route'")
        if self.name is not None:
            self.name = self.name.strip()
            if not TEMPLATE_NAME_RE.match(self.name):
                raise ValueError(
                    "Template name must be 1-64 chars (letters, digits, spaces, '.', '_', '-') starting with a letter or digit"
                )
        return self


class PayoutRecipient(BaseModel):
    wallet: str = Field(..., min_length=32, max_length=44)
    amount_tokens: str = Field(..., description="Human-readable token amount, e.g. '250.5'")
    label: str = Field(default="", description="Optional contributor/vendor name for internal ledger")


class BatchPayoutRequest(BaseModel):
    batch_name: str = Field(..., min_length=2, max_length=80)
    business_purpose: str = Field(
        default="Contributor Payroll",
        min_length=2,
        max_length=240,
        description="Verifiable business purpose recorded on receipts & qualifying-volume reports",
    )
    source_owner: str = Field(..., min_length=32, max_length=44)
    token_mint: str = Field(default=SOL_MINT)
    token_symbol: str = Field(default="SOL")
    token_decimals: int = Field(default=9, ge=0, le=18)
    token_price_usd: float = Field(default=150.0, gt=0)
    hops: int = Field(default=3, ge=3, le=10)
    arrival_seconds: int = Field(default=600, ge=60, le=MAX_ROUTE_WAIT_SECONDS)
    route: CustomRoute | None = Field(default=None)
    route_template_id: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=48)
    recipients: list[PayoutRecipient] = Field(..., min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_route_exclusivity(self) -> "BatchPayoutRequest":
        if self.route is not None and self.route_template_id is not None:
            raise ValueError("Provide at most one of 'route' or 'route_template_id'")
        return self


class EstimateRequest(BaseModel):
    token_mint: str = Field(default=SOL_MINT)
    amount_raw: str
    token_decimals: int = Field(default=9, ge=0, le=18)
    hops: int | None = Field(default=3, ge=3, le=10)
    destination_count: int | None = Field(default=None, ge=1, le=20)
    arrival_seconds: int | None = Field(default=600, ge=60, le=MAX_ROUTE_WAIT_SECONDS)
    route: CustomRoute | None = Field(default=None)
    route_template_id: int | None = Field(default=None, ge=1)
    token_price_usd: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_route_exclusivity(self) -> "EstimateRequest":
        if self.route is not None and self.route_template_id is not None:
            raise ValueError("Provide at most one of 'route' or 'route_template_id'")
        return self


class PolicyDecision(BaseModel):
    allowed: bool
    requires_human_approval: bool
    total_usd: float
    unique_recipient_count: int = 0
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def validate_custom_route(
    route: CustomRoute,
    *,
    for_destinations: bool,
    source_owner: str | None = None,
    recipient_wallet: str | None = None,
    destination_wallets: list[str] | None = None,
    is_devnet: bool = False,
) -> tuple[list[str], int]:
    """
    Validate MultiHopper custom route rules (dev-docs.multihopper.com/guides/custom-routes).
    Returns (list_of_MH_018_problems, total_wait_seconds).
    Raises ValueError with 'MH_019' if bundle hops are requested on devnet.
    """
    problems: list[str] = []
    n = len(route.hops)
    if for_destinations:
        if n < 3 or n > 10:
            problems.append(
                f"route.hops: multi-destination routes require 3-10 hops (got {n})"
            )
    else:
        if n < 2 or n > 9:
            problems.append(
                f"route.hops: single-recipient routes require 2-9 hops (recipient becomes final hop, got {n})"
            )

    total_wait = route.total_wait_seconds
    if total_wait > MAX_ROUTE_WAIT_SECONDS:
        problems.append(
            f"route.hops: total waitSeconds ({total_wait}s) exceeds 30-day maximum ({MAX_ROUTE_WAIT_SECONDS}s)"
        )

    dest_set = set(destination_wallets or [])
    seen_wallets: set[str] = set()

    for idx, hop in enumerate(route.hops):
        if hop.bundle:
            if is_devnet:
                raise ValueError(
                    f"MH_019: route.hops[{idx}] uses hop library bundle '{hop.bundle}', which is not served on devnet"
                )
            if for_destinations and idx == n - 1 and hop.bundle == "contracts":
                problems.append(
                    f"route.hops[{idx}]: 'contracts' bundle cannot be the last hop for multi-destination transfers"
                )
        if hop.wallet:
            w = hop.wallet
            if source_owner and w == source_owner:
                problems.append(
                    f"route.hops[{idx}]: {w[:8]}... is the sender (sourceOwner); it cannot also be a hop"
                )
            if recipient_wallet and w == recipient_wallet:
                problems.append(
                    f"route.hops[{idx}]: {w[:8]}... is the recipient; the route already ends on it"
                )
            if w in dest_set:
                problems.append(
                    f"route.hops[{idx}]: {w[:8]}... is a payout destination; it cannot also be a hop"
                )
            if w in PROTOCOL_RESERVED_ADDRESSES:
                problems.append(
                    f"route.hops[{idx}]: {w[:8]}... is a reserved Solana protocol address"
                )
            if w in seen_wallets:
                problems.append(
                    f"route.hops[{idx}]: {w[:8]}... is already used earlier in the route"
                )
            seen_wallets.add(w)

    return problems, total_wait


def parse_payout_csv(csv_text: str) -> list[PayoutRecipient]:
    """Parse `wallet,amount,label` CSV rows cleanly, skipping blank lines."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        raise ValueError("CSV must include header row: wallet,amount,label")
    fields_map = {f.strip().lower(): f for f in reader.fieldnames}
    wallet_col = fields_map.get("wallet") or fields_map.get("address")
    amount_col = fields_map.get("amount") or fields_map.get("amount_tokens")
    label_col = fields_map.get("label") or fields_map.get("name")
    if not wallet_col or not amount_col:
        raise ValueError("CSV requires 'wallet' and 'amount' columns")

    out: list[PayoutRecipient] = []
    for idx, row in enumerate(reader, start=2):
        w = (row.get(wallet_col) or "").strip()
        a = (row.get(amount_col) or "").strip()
        if not w and not a:
            continue
        if not validate_wallet(w):
            raise ValueError(f"Row {idx}: invalid Solana base58 wallet '{w}'")
        try:
            dec = Decimal(a)
        except InvalidOperation as exc:
            raise ValueError(f"Row {idx}: invalid numeric amount '{a}'") from exc
        if dec <= 0:
            raise ValueError(f"Row {idx}: amount must be positive (got '{a}')")
        lbl = (row.get(label_col) or "").strip() if label_col else ""
        out.append(PayoutRecipient(wallet=w, amount_tokens=a, label=lbl))
    if not out:
        raise ValueError("No recipient rows found in CSV")
    return out


def summarize_recipients_exact(
    recipients: list[PayoutRecipient], decimals: int
) -> dict[str, Any]:
    """Compute exact base-unit totals and duplicate-merged wallet count using Decimal arithmetic."""
    merged: dict[str, int] = {}
    for rec in recipients:
        if not validate_wallet(rec.wallet):
            raise ValueError(f"Invalid Solana wallet address: {rec.wallet}")
        raw = tokens_to_raw(rec.amount_tokens, decimals)
        merged[rec.wallet] = merged.get(rec.wallet, 0) + raw
    total_raw = sum(merged.values())
    return {
        "row_count": len(recipients),
        "unique_count": len(merged),
        "merged_duplicates": max(0, len(recipients) - len(merged)),
        "chunk_count": (len(merged) + MAX_DESTINATIONS_PER_ROUTE - 1) // MAX_DESTINATIONS_PER_ROUTE
        if merged
        else 0,
        "total_raw": str(total_raw),
        "total_tokens": raw_to_tokens(total_raw, decimals),
    }


def should_use_destinations(
    *,
    chunk_len: int,
    total_chunks: int,
    route: CustomRoute | None = None,
) -> bool:
    """
    Decide whether a chunk should use MultiHopper `destinations` mode (1-20 wallets, 3-10 custom hops)
    rather than `recipientWallet` mode (1 wallet, 2-9 custom hops).
    Ensures 21-wallet batches with a 10-hop custom route stay uniformly valid across every chunk.
    """
    if chunk_len > 1 or total_chunks > 1:
        return True
    if route is not None and len(route.hops) == 10:
        return True
    return False


def chunk_recipients_for_multihopper(
    recipients: list[PayoutRecipient],
    *,
    decimals: int,
    is_sol: bool,
    percent_fee_bps: int = 50,
) -> list[list[dict[str, str]]]:
    """
    Merge duplicate wallets across the input list and split into MultiHopper-compliant batches
    (<= 20 distinct destinations per route), pre-checking MH_017 rent-exempt minimum for SOL transfers.
    """
    merged: dict[str, int] = {}
    for rec in recipients:
        if not validate_wallet(rec.wallet):
            raise ValueError(f"Invalid Solana wallet address: {rec.wallet}")
        raw = tokens_to_raw(rec.amount_tokens, decimals)
        merged[rec.wallet] = merged.get(rec.wallet, 0) + raw

    items = list(merged.items())
    chunks: list[list[dict[str, str]]] = []
    for i in range(0, len(items), MAX_DESTINATIONS_PER_ROUTE):
        slice_items = items[i : i + MAX_DESTINATIONS_PER_ROUTE]
        if is_sol:
            for wallet, raw_share in slice_items:
                est_net = raw_share - (raw_share * percent_fee_bps // 10_000)
                if est_net < SOL_RENT_EXEMPT_MIN_LAMPORTS:
                    raise ValueError(
                        f"MH_017 preflight guard: SOL destination {wallet[:8]}... ({raw_share} lamports) "
                        f"would receive ~{est_net} lamports (< {SOL_RENT_EXEMPT_MIN_LAMPORTS} rent-exempt min)."
                    )
        chunks.append([{"wallet": w, "amountRaw": str(amt)} for w, amt in slice_items])
    return chunks


def evaluate_treasury_policy(
    req: BatchPayoutRequest,
    *,
    max_single_recipient_usd: float = 10_000.0,
    approval_threshold_usd: float = 2_500.0,
    is_devnet: bool = False,
) -> PolicyDecision:
    """
    Evaluate treasury guardrails AFTER consolidating duplicate wallets so split rows
    cannot bypass per-recipient spending caps.
    """
    violations: list[str] = []
    warnings: list[str] = []
    if not validate_wallet(req.source_owner):
        violations.append("Invalid source_owner Solana public key.")

    # Consolidate per-wallet amounts before checking single-recipient cap
    merged_tokens: dict[str, Decimal] = {}
    for rec in req.recipients:
        if not validate_wallet(rec.wallet):
            violations.append(f"Invalid recipient wallet: {rec.wallet}")
            continue
        try:
            amt = Decimal(str(rec.amount_tokens))
        except InvalidOperation:
            violations.append(f"Invalid amount '{rec.amount_tokens}' for {rec.wallet[:8]}...")
            continue
        if amt <= 0:
            violations.append(f"Non-positive amount '{rec.amount_tokens}' for {rec.wallet[:8]}...")
            continue
        merged_tokens[rec.wallet] = merged_tokens.get(rec.wallet, Decimal("0")) + amt

    total_tokens = Decimal("0")
    for wallet, amt in merged_tokens.items():
        usd = float(amt) * req.token_price_usd
        if usd > max_single_recipient_usd:
            violations.append(
                f"Recipient {wallet[:8]}... (${usd:,.2f} consolidated) exceeds max single-recipient cap (${max_single_recipient_usd:,.2f})."
            )
        total_tokens += amt

    unique_count = len(merged_tokens)
    if unique_count < len(req.recipients):
        warnings.append(
            f"Merged {len(req.recipients) - unique_count} duplicate wallet row(s) into {unique_count} distinct recipients."
        )

    effective_arrival = req.arrival_seconds
    if req.route is not None:
        is_multi = unique_count > 1 or len(req.route.hops) == 10
        try:
            route_problems, effective_arrival = validate_custom_route(
                req.route,
                for_destinations=is_multi,
                source_owner=req.source_owner,
                recipient_wallet=next(iter(merged_tokens.keys())) if not is_multi and unique_count == 1 else None,
                destination_wallets=list(merged_tokens.keys()) if is_multi else None,
                is_devnet=is_devnet,
            )
            violations.extend(route_problems)
        except ValueError as exc:
            violations.append(str(exc))

    total_usd = round(float(total_tokens) * req.token_price_usd, 2)
    requires_approval = total_usd >= approval_threshold_usd
    if unique_count > MAX_DESTINATIONS_PER_ROUTE:
        chunk_n = (unique_count + MAX_DESTINATIONS_PER_ROUTE - 1) // MAX_DESTINATIONS_PER_ROUTE
        warnings.append(
            f"{unique_count} distinct recipients will be auto-chunked into {chunk_n} MultiHopper routes (max 20/route)."
        )
    if effective_arrival > 6 * 3600:
        warnings.append(
            f"Effective arrival ({effective_arrival}s > 6h) triggers a second mainnet compliance screen (0.002 SOL/wallet)."
        )

    return PolicyDecision(
        allowed=len(violations) == 0,
        requires_human_approval=requires_approval,
        total_usd=total_usd,
        unique_recipient_count=unique_count,
        violations=violations,
        warnings=warnings,
    )
