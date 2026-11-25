"""HMAC-SHA256 webhook verification, approval audit log, and SQLite persistence for HopPay Treasury."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

from hoppay.models import utc_now_iso

WEBHOOK_STATUS_MAP: dict[str, str] = {
    "transfer.quote_created": "quote",
    "transfer.deposit_confirmed": "funding_confirmed",
    "transfer.processing": "processing",
    "transfer.phase_changed": "processing",
    "transfer.hop_complete": "active",
    "transfer.completed": "completed",
    "transfer.failed": "failed",
    "transfer.expired": "expired",
    "transfer.refunded": "refunded",
    "transfer.recoverable": "recoverable",
    "transfer.rescued": "rescued",
    "transfer.rent_reclaimed": "reclaimed",
}


def sign_webhook_payload(raw_body: bytes, secret: str) -> str:
    """Compute MultiHopper's exact `x-multihopper-signature` hex HMAC-SHA256 digest."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_multihopper_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = sign_webhook_payload(raw_body, secret)
    return hmac.compare_digest(signature.strip().lower(), expected.lower())


def aggregate_batch_status(
    transfers: list[dict[str, Any]],
    *,
    current_status: str = "quoted",
    requires_approval: bool = False,
    approved_at: str = "",
) -> str:
    """Compute deterministic aggregate batch status across all chunked transfers."""
    if current_status == "rejected":
        return "rejected"
    if requires_approval and not approved_at:
        return "awaiting_approval"
    if not transfers:
        return current_status

    statuses = [str(t.get("status") or "quote") for t in transfers]
    if all(s == "completed" for s in statuses):
        return "completed"
    if all(s == "refunded" for s in statuses):
        return "refunded"
    if all(s == "reclaimed" for s in statuses):
        return "reclaimed"
    if all(s == "rescued" for s in statuses):
        return "rescued"
    if any(s == "recoverable" for s in statuses):
        return "recoverable"
    if all(s in ("failed", "expired") for s in statuses):
        return "failed"
    if any(s in ("failed", "expired") for s in statuses):
        return "partial_failed"
    if any(s in ("active", "processing") for s in statuses):
        return "processing"
    if any(s == "funding_confirmed" for s in statuses):
        return "funding_confirmed"
    if any(s == "awaiting_signature" for s in statuses):
        return "awaiting_signature"
    if any(s == "completed" for s in statuses):
        return "processing"
    return "approved" if (requires_approval and approved_at) else "quoted"


class HopPayStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS payout_batches (
                    id TEXT PRIMARY KEY,
                    batch_name TEXT NOT NULL,
                    business_purpose TEXT NOT NULL DEFAULT 'Contributor Payroll',
                    source_owner TEXT NOT NULL,
                    token_mint TEXT NOT NULL,
                    token_symbol TEXT NOT NULL,
                    token_decimals INTEGER NOT NULL,
                    recipient_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    total_usd REAL NOT NULL,
                    estimated_integrator_rev_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    requires_approval INTEGER NOT NULL DEFAULT 0,
                    approved_by TEXT NOT NULL DEFAULT '',
                    approved_at TEXT NOT NULL DEFAULT '',
                    routing_mode TEXT NOT NULL DEFAULT 'standard',
                    route_config_json TEXT NOT NULL DEFAULT '{}',
                    transfers_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS integrator_payouts (
                    id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                """
            )
            # Safe additive migrations for pre-existing local SQLite databases
            existing_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(payout_batches)").fetchall()
            }
            migrations = [
                ("business_purpose", "TEXT NOT NULL DEFAULT 'Contributor Payroll'"),
                ("approved_by", "TEXT NOT NULL DEFAULT ''"),
                ("approved_at", "TEXT NOT NULL DEFAULT ''"),
                ("routing_mode", "TEXT NOT NULL DEFAULT 'standard'"),
                ("route_config_json", "TEXT NOT NULL DEFAULT '{}'"),
            ]
            for col_name, col_ddl in migrations:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE payout_batches ADD COLUMN {col_name} {col_ddl}")

    def save_batch(
        self,
        *,
        batch_id: str,
        batch_name: str,
        business_purpose: str = "Contributor Payroll",
        source_owner: str,
        token_mint: str,
        token_symbol: str,
        token_decimals: int,
        recipient_count: int,
        chunk_count: int,
        total_usd: float,
        estimated_integrator_rev_usd: float,
        status: str,
        requires_approval: bool,
        transfers: list[dict[str, Any]],
        routing_mode: str = "standard",
        route_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO payout_batches (
                    id, batch_name, business_purpose, source_owner, token_mint, token_symbol, token_decimals,
                    recipient_count, chunk_count, total_usd, estimated_integrator_rev_usd,
                    status, requires_approval, approved_by, approved_at,
                    routing_mode, route_config_json, transfers_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    batch_name,
                    business_purpose,
                    source_owner,
                    token_mint,
                    token_symbol,
                    token_decimals,
                    recipient_count,
                    chunk_count,
                    total_usd,
                    estimated_integrator_rev_usd,
                    status,
                    1 if requires_approval else 0,
                    "",
                    "",
                    routing_mode,
                    json.dumps(route_config or {}),
                    json.dumps(transfers),
                    now,
                    now,
                ),
            )
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payout_batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            return self._row_to_batch(row)

    def find_batch_by_transfer_id(self, transfer_id: int | str) -> dict[str, Any] | None:
        tid = str(transfer_id)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM payout_batches ORDER BY created_at DESC").fetchall()
            for row in rows:
                batch = self._row_to_batch(row)
                if any(str(t.get("id")) == tid for t in batch["transfers"]):
                    return batch
        return None

    def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payout_batches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_batch(r) for r in rows]

    def approve_batch(self, batch_id: str, approver: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payout_batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = self._row_to_batch(row)
            if batch["status"] == "rejected":
                raise ValueError("Cannot approve a rejected batch")
            new_status = aggregate_batch_status(
                batch["transfers"],
                current_status="approved",
                requires_approval=batch["requires_approval"],
                approved_at=now,
            )
            conn.execute(
                """
                UPDATE payout_batches
                SET approved_by = ?, approved_at = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (approver.strip() or "treasury_approver", now, new_status, now, batch_id),
            )
        return self.get_batch(batch_id)

    def reject_batch(self, batch_id: str, reason: str = "") -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payout_batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            cfg = json.loads(row["route_config_json"] or "{}")
            if reason:
                cfg["rejection_reason"] = reason
            conn.execute(
                "UPDATE payout_batches SET status = 'rejected', route_config_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cfg), now, batch_id),
            )
        return self.get_batch(batch_id)

    def sync_transfer_state(self, transfer_id: int | str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Merge fresh MultiHopper transfer telemetry into its parent batch and recompute batch status."""
        tid = str(transfer_id)
        now = utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM payout_batches").fetchall()
            for row in rows:
                transfers = json.loads(row["transfers_json"] or "[]")
                changed = False
                for t in transfers:
                    if str(t.get("id")) == tid:
                        for k, v in updates.items():
                            if v is not None:
                                t[k] = v
                        changed = True
                if changed:
                    batch_st = aggregate_batch_status(
                        transfers,
                        current_status=row["status"],
                        requires_approval=bool(row["requires_approval"]),
                        approved_at=row["approved_at"],
                    )
                    conn.execute(
                        "UPDATE payout_batches SET transfers_json = ?, status = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(transfers), batch_st, now, row["id"]),
                    )
                    return self.get_batch(row["id"])
        return None

    def replace_transfer_in_batch(
        self,
        batch_id: str,
        old_transfer_id: int | str,
        new_transfer: dict[str, Any],
        *,
        reason: str = "MH_040_requote",
        extra_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Replace a stale/expired transfer inside a batch (e.g. after MH_040 tier shift) while archiving the old ID."""
        now = utc_now_iso()
        old_tid = str(old_transfer_id)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payout_batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            transfers = json.loads(row["transfers_json"] or "[]")
            cfg = json.loads(row["route_config_json"] or "{}")
            superseded = list(cfg.get("superseded_transfers") or [])
            replaced = False
            for idx, t in enumerate(transfers):
                if str(t.get("id")) == old_tid:
                    superseded.append(
                        {
                            "old_transfer_id": t.get("id"),
                            "new_transfer_id": new_transfer.get("id"),
                            "reason": reason,
                            "replaced_at": now,
                        }
                    )
                    transfers[idx] = new_transfer
                    replaced = True
                    break
            if not replaced:
                return None
            cfg["superseded_transfers"] = superseded
            if extra_config:
                cfg.update(extra_config)
            batch_st = aggregate_batch_status(
                transfers,
                current_status=row["status"],
                requires_approval=bool(row["requires_approval"]),
                approved_at=row["approved_at"],
            )
            conn.execute(
                "UPDATE payout_batches SET transfers_json = ?, route_config_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(transfers), json.dumps(cfg), batch_st, now, batch_id),
            )
        return self.get_batch(batch_id)

    @staticmethod
    def _row_to_batch(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "batch_name": r["batch_name"],
            "business_purpose": r["business_purpose"],
            "source_owner": r["source_owner"],
            "token_mint": r["token_mint"],
            "token_symbol": r["token_symbol"],
            "token_decimals": r["token_decimals"],
            "recipient_count": r["recipient_count"],
            "chunk_count": r["chunk_count"],
            "total_usd": r["total_usd"],
            "estimated_integrator_rev_usd": r["estimated_integrator_rev_usd"],
            "status": r["status"],
            "requires_approval": bool(r["requires_approval"]),
            "approved_by": r["approved_by"],
            "approved_at": r["approved_at"],
            "routing_mode": r["routing_mode"],
            "route_config": json.loads(r["route_config_json"] or "{}"),
            "transfers": json.loads(r["transfers_json"] or "[]"),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    def record_webhook_event(self, event_type: str, transfer_id: str, payload: dict[str, Any]) -> bool:
        """Idempotently store webhook event; returns True if new, False if duplicate replay."""
        digest = hashlib.sha256(
            f"{event_type}:{transfer_id}:{json.dumps(payload, sort_keys=True)}".encode("utf-8")
        ).hexdigest()[:32]
        event_key = f"{event_type}:{transfer_id}:{digest}"
        now = utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO webhook_events (event_key, event_type, transfer_id, payload_json, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_key, event_type, str(transfer_id), json.dumps(payload), now),
            )
            if cur.rowcount == 0:
                return False

            if event_type == "payout.completed":
                conn.execute(
                    "INSERT OR IGNORE INTO integrator_payouts (id, payload_json, received_at) VALUES (?, ?, ?)",
                    (event_key, json.dumps(payload), now),
                )
                return True

            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            new_st = WEBHOOK_STATUS_MAP.get(event_type)
            if new_st:
                rows = conn.execute("SELECT * FROM payout_batches").fetchall()
                for row in rows:
                    transfers = json.loads(row["transfers_json"] or "[]")
                    changed = False
                    for t in transfers:
                        if str(t.get("id")) == str(transfer_id):
                            # Preserve terminal completed state unless explicitly rescued/reclaimed
                            if event_type == "transfer.phase_changed" and t.get("status") == "completed":
                                pass
                            else:
                                t["status"] = new_st
                            if data.get("phase"):
                                t["phase"] = data["phase"]
                            if isinstance(data.get("progress"), dict):
                                t["progress"] = data["progress"]
                            if data.get("recovery") is not None:
                                t["recovery"] = data["recovery"]
                            if data.get("error") or data.get("lastError"):
                                t["lastError"] = data.get("lastError") or data.get("error")
                            tx_sig = data.get("txSignature")
                            if tx_sig:
                                sigs = t.get("signatures") if isinstance(t.get("signatures"), dict) else {}
                                hops_sigs = list(sigs.get("hops") or [])
                                if tx_sig not in hops_sigs:
                                    hops_sigs.append(tx_sig)
                                sigs["hops"] = hops_sigs
                                t["signatures"] = sigs
                            if event_type == "transfer.completed":
                                t["completedAt"] = t.get("completedAt") or now
                                if isinstance(t.get("destinations"), list):
                                    for idx_d, d in enumerate(t["destinations"]):
                                        if d.get("status") != "paid":
                                            d["status"] = "paid"
                                            d["paidAt"] = d.get("paidAt") or now
                                            d["payoutSignature"] = (
                                                d.get("payoutSignature")
                                                or tx_sig
                                                or f"wh_settled_{transfer_id}_{idx_d}"
                                            )
                            changed = True
                    if changed:
                        batch_st = aggregate_batch_status(
                            transfers,
                            current_status=row["status"],
                            requires_approval=bool(row["requires_approval"]),
                            approved_at=row["approved_at"],
                        )
                        conn.execute(
                            "UPDATE payout_batches SET transfers_json = ?, status = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(transfers), batch_st, now, row["id"]),
                        )
            return True

    def list_webhook_events(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                {
                    "event_key": r["event_key"],
                    "event_type": r["event_type"],
                    "transfer_id": r["transfer_id"],
                    "payload": json.loads(r["payload_json"]),
                    "received_at": r["received_at"],
                }
                for r in rows
            ]

    def get_summary(self) -> dict[str, Any]:
        batches = self.list_batches(limit=500)
        non_rejected = [b for b in batches if b["status"] != "rejected"]
        completed = [b for b in non_rejected if b["status"] == "completed"]
        total_volume_usd = round(sum(b["total_usd"] for b in non_rejected), 2)
        settled_volume_usd = round(sum(b["total_usd"] for b in completed), 2)
        total_rev_usd = round(sum(b["estimated_integrator_rev_usd"] for b in non_rejected), 4)
        settled_rev_usd = round(sum(b["estimated_integrator_rev_usd"] for b in completed), 4)
        with self._connect() as conn:
            payout_claims_count = int(
                conn.execute("SELECT COUNT(*) FROM integrator_payouts").fetchone()[0]
            )
        return {
            "batch_count": len(non_rejected),
            "completed_batches": len(completed),
            "pending_approval_count": sum(1 for b in non_rejected if b["status"] == "awaiting_approval"),
            "total_recipients": sum(b["recipient_count"] for b in non_rejected),
            "total_volume_usd": total_volume_usd,
            "settled_volume_usd": settled_volume_usd,
            "estimated_integrator_rev_usd": total_rev_usd,
            "settled_integrator_rev_usd": settled_rev_usd,
            "challenge_threshold_pct": round(min(100.0, (total_volume_usd / 100_000.0) * 100.0), 2),
            "settled_threshold_pct": round(min(100.0, (settled_volume_usd / 100_000.0) * 100.0), 2),
            "payout_claims_count": payout_claims_count,
        }
