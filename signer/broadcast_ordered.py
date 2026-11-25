"""
Non-custodial Python Solana signer & ordered broadcaster for MultiHopper v1 API.

Designed for autonomous AI agent treasuries and Python backends using `solders` + `httpx`.
Preserves server ephemeral partial signatures on VersionedTransaction (v0) and enforces
MultiHopper's strict two-stage confirm-broadcast order + 3s RPC propagation waits.

Install optional signer deps:
    pip install solders base58 httpx

Usage:
    MULTIHOPPER_ENV=devnet MULTIHOPPER_API_KEY=mh_test_... \
    SOLANA_PRIVATE_KEY=<base58_or_json_path> python signer/broadcast_ordered.py <transfer_id> [--poll]
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


def load_keypair(spec: str):
    """Load a solders Keypair from either a Solana JSON keypair file path or base58 secret string."""
    import base58
    from solders.keypair import Keypair

    p = Path(spec).expanduser()
    if p.exists() and p.is_file():
        raw = bytes(json.loads(p.read_text(encoding="utf-8")))
        return Keypair.from_bytes(raw)
    return Keypair.from_bytes(base58.b58decode(spec.strip()))


def sign_versioned(base64_tx: str, keypair) -> str:
    """Sign a VersionedTransaction (v0) without overwriting existing server partial signatures."""
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(base64.b64decode(base64_tx))
    msg_bytes = bytes([0x80]) + bytes(tx.message)
    our_sig = keypair.sign_message(msg_bytes)
    account_keys = list(tx.message.account_keys)
    idx = next(i for i, k in enumerate(account_keys) if k == keypair.pubkey())
    sigs = list(tx.signatures)
    sigs[idx] = our_sig
    return base64.b64encode(bytes(VersionedTransaction.populate(tx.message, sigs))).decode("ascii")


def sign_legacy(base64_tx: str, keypair) -> str:
    """Sign a legacy Solana Transaction (used by orchestratorInitTx)."""
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(base64_tx))
    tx.partial_sign([keypair], tx.message.recent_blockhash)
    return base64.b64encode(bytes(tx)).decode("ascii")


def rpc_send_and_confirm(signed_b64: str, label: str, rpc_url: str) -> str:
    resp = httpx.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "skipPreflight": False}],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{label} RPC send failed: {data['error']}")
    sig = str(data["result"])
    print(f"  {label}: {sig}")

    for _ in range(15):
        time.sleep(3)
        st = httpx.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": True}],
            },
            timeout=30.0,
        ).json()
        val = (st.get("result", {}).get("value") or [None])[0]
        if val and val.get("err"):
            raise RuntimeError(f"{label} failed on-chain: {val['err']}")
        if val and val.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig
    return sig


def poll_until_settled(
    transfer_id: int,
    *,
    api_base: str,
    api_key: str,
    max_polls: int = 60,
    interval_sec: float = 5.0,
) -> dict[str, Any] | None:
    print(f"\nPolling GET /transfers/{transfer_id} until terminal state...")
    for idx in range(1, max_polls + 1):
        r = httpx.get(
            f"{api_base}/transfers/{transfer_id}",
            headers={"x-api-key": api_key},
            timeout=30.0,
        )
        tr = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"GET /transfers/{transfer_id} failed: {tr}")
        prog = tr.get("progress") or {}
        hops = f"{prog.get('hopsCompleted', '-')}/{prog.get('hopsTotal', '-')}"
        print(f"  [{idx}] status={tr.get('status')} phase={tr.get('phase') or '-'} hops={hops}")
        if tr.get("status") in ("completed", "failed", "expired", "refunded"):
            return tr
        time.sleep(interval_sec)
    print("Polling window reached; check transfer status later.")
    return None


def sign_and_broadcast_transfer(
    transfer_id: int,
    *,
    keypair_spec: str,
    api_key: str,
    env: str = "devnet",
    rpc_url: str | None = None,
    poll: bool = False,
) -> dict[str, Any]:
    api_base = (
        "https://multihopper.com/api/v1"
        if env.lower() == "mainnet"
        else "https://devnet.multihopper.com/api/v1"
    )
    rpc = rpc_url or (
        "https://api.mainnet-beta.solana.com"
        if env.lower() == "mainnet"
        else "https://api.devnet.solana.com"
    )
    kp = load_keypair(keypair_spec)

    def mh_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        if method in ("POST", "PATCH", "DELETE"):
            headers["Idempotency-Key"] = str(uuid.uuid4())
        r = httpx.request(method, f"{api_base}{path}", json=body, headers=headers, timeout=30.0)
        payload = r.json()
        if r.status_code >= 400:
            err = payload.get("error") or {}
            raise RuntimeError(f"[{r.status_code} {err.get('code', 'HTTP_ERROR')}] {err.get('message', r.text)}")
        return payload

    route_sigs: list[str] = []
    session_sigs: list[str] = []
    dispersion_sigs: list[str] = []
    keeper_sig: str | None = None
    orch_sig: str | None = None

    for attempt in range(1, 5):
        print(f"\n[Attempt {attempt}/4] POST /transfers/{transfer_id}/prepare")
        prep = mh_request("POST", f"/transfers/{transfer_id}/prepare")
        ptx = prep["preparedTxs"]
        if (ptx.get("resume") or {}).get("nothingToDo"):
            print("All on-chain accounts already initialized.")
            current = mh_request("GET", f"/transfers/{transfer_id}")
            if current.get("status") in ("awaiting_signature", "funding_confirmed") and route_sigs:
                final_body: dict[str, Any] = {"routeInitSignatures": route_sigs}
                if keeper_sig:
                    final_body["keeperFundingSignature"] = keeper_sig
                if orch_sig:
                    final_body["orchestratorInitSignature"] = orch_sig
                if session_sigs:
                    final_body["sessionInitSignatures"] = session_sigs
                if dispersion_sigs:
                    final_body["dispersionInitSignatures"] = dispersion_sigs
                current = mh_request("POST", f"/transfers/{transfer_id}/confirm-broadcast", final_body)
            if poll:
                return poll_until_settled(transfer_id, api_base=api_base, api_key=api_key) or current
            return current

        try:
            if ptx.get("keeperFundingTx"):
                signed = sign_versioned(ptx["keeperFundingTx"], kp)
                keeper_sig = rpc_send_and_confirm(signed, "1/5 keeperFundingTx", rpc)
                mh_request(
                    "POST",
                    f"/transfers/{transfer_id}/confirm-broadcast",
                    {"routeInitSignatures": [], "keeperFundingSignature": keeper_sig},
                )
                print("  -> Intermediate confirm-broadcast recorded.")

            r_list = ptx.get("routeInitTxs") or []
            for idx, item in enumerate(r_list):
                b64 = item if isinstance(item, str) else item["base64"]
                route_sigs.append(rpc_send_and_confirm(sign_versioned(b64, kp), f"2/5 routeInitTxs[{idx}]", rpc))
                if idx < len(r_list) - 1:
                    time.sleep(3)
            if r_list:
                time.sleep(3)

            if ptx.get("orchestratorInitTx"):
                orch_sig = rpc_send_and_confirm(
                    sign_legacy(ptx["orchestratorInitTx"], kp), "3/5 orchestratorInitTx", rpc
                )
                time.sleep(3)

            for idx, b64 in enumerate(ptx.get("sessionInitTxs") or []):
                session_sigs.append(
                    rpc_send_and_confirm(sign_versioned(b64, kp), f"4/5 sessionInitTxs[{idx}]", rpc)
                )

            for idx, b64 in enumerate(ptx.get("dispersionInitTxs") or []):
                dispersion_sigs.append(
                    rpc_send_and_confirm(sign_versioned(b64, kp), f"5/5 dispersionInitTxs[{idx}]", rpc)
                )

            final_body = {"routeInitSignatures": route_sigs}
            if keeper_sig:
                final_body["keeperFundingSignature"] = keeper_sig
            if orch_sig:
                final_body["orchestratorInitSignature"] = orch_sig
            if session_sigs:
                final_body["sessionInitSignatures"] = session_sigs
            if dispersion_sigs:
                final_body["dispersionInitSignatures"] = dispersion_sigs

            result = mh_request("POST", f"/transfers/{transfer_id}/confirm-broadcast", final_body)
            print("Confirmed! Transfer advanced:", json.dumps(result))
            if poll:
                poll_until_settled(transfer_id, api_base=api_base, api_key=api_key)
            return result
        except RuntimeError as exc:
            msg = str(exc)
            retryable = any(
                token in msg
                for token in (
                    "MH_041",
                    "Blockhash not found",
                    "block height exceeded",
                    "TransactionExpiredBlockheightExceededError",
                )
            )
            if retryable and attempt < 4:
                print(f"Resumable error ({msg}); retrying via /prepare...")
                time.sleep(2)
                continue
            raise
    return {}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python signer/broadcast_ordered.py <transfer_id> [--poll]")
        sys.exit(1)
    api_key = os.environ.get("MULTIHOPPER_API_KEY", "")
    kp_spec = os.environ.get("SOLANA_PRIVATE_KEY") or os.environ.get("SOLANA_KEYPAIR_PATH", "")
    if not api_key or not kp_spec:
        print("Set MULTIHOPPER_API_KEY and SOLANA_PRIVATE_KEY (or SOLANA_KEYPAIR_PATH)")
        sys.exit(1)
    sign_and_broadcast_transfer(
        int(sys.argv[1]),
        keypair_spec=kp_spec,
        api_key=api_key,
        env=os.environ.get("MULTIHOPPER_ENV", "devnet"),
        rpc_url=os.environ.get("SOLANA_RPC_URL"),
        poll="--poll" in sys.argv[2:],
    )
