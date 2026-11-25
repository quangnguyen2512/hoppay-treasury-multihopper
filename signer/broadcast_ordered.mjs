/**
 * Non-custodial Solana client-side signer & ordered broadcaster for MultiHopper v1 API.
 *
 * Enforces the exact official broadcast order & 3-second RPC propagation waits documented at:
 *   https://dev-docs.multihopper.com/guides/agentic-integration
 *   https://dev-docs.multihopper.com/guides/multi-destination
 *
 * Order:
 *   1. keeperFundingTx -> confirm -> intermediate POST /confirm-broadcast (prevents double-funding)
 *   2. routeInitTxs[] (sequentially, wait confirmation + 3s propagation)
 *   3. orchestratorInitTx (legacy Transaction, partialSign + requireAllSignatures:false + 3s propagation)
 *   4. sessionInitTxs[] (VersionedTransaction, preserves server partial sigs)
 *   5. dispersionInitTxs[] (multi-destination payout orchestrator + step PDAs + exit binding)
 *   6. final POST /confirm-broadcast (auto-resumes via /prepare on MH_041 or blockhash expiry)
 *
 * Private key NEVER leaves this local signer process.
 * Usage:
 *   cd signer && npm install
 *   MULTIHOPPER_ENV=devnet MULTIHOPPER_API_KEY=mh_test_... \
 *   SOLANA_KEYPAIR_PATH=~/.config/solana/devnet.json node broadcast_ordered.mjs <transferId> [--poll]
 */

import fs from "node:fs";
import crypto from "node:crypto";

const ENV = (process.env.MULTIHOPPER_ENV || "devnet").toLowerCase();
const API_BASE =
  ENV === "mainnet"
    ? "https://multihopper.com/api/v1"
    : "https://devnet.multihopper.com/api/v1";
const API_KEY = process.env.MULTIHOPPER_API_KEY || "";
const RPC_URL =
  process.env.SOLANA_RPC_URL ||
  (ENV === "mainnet"
    ? "https://api.mainnet-beta.solana.com"
    : "https://api.devnet.solana.com");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function mhRequest(method, path, body = undefined) {
  const headers = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
  };
  if (method === "POST" || method === "PATCH" || method === "DELETE") {
    headers["Idempotency-Key"] = crypto.randomUUID();
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  if (!res.ok) {
    const err = new Error(json?.error?.message || `HTTP ${res.status}`);
    err.code = json?.error?.code || "HTTP_ERROR";
    err.status = res.status;
    throw err;
  }
  return json;
}

export async function signAndBroadcastTransfer(transferId, keypairPath, { poll = false } = {}) {
  if (!API_KEY) throw new Error("MULTIHOPPER_API_KEY is required for live signing");
  const { Connection, Keypair, Transaction, VersionedTransaction } = await import(
    "@solana/web3.js"
  );
  const secret = Uint8Array.from(JSON.parse(fs.readFileSync(keypairPath, "utf8")));
  const signer = Keypair.fromSecretKey(secret);
  const connection = new Connection(RPC_URL, "confirmed");

  async function sendVersioned(b64, label) {
    const tx = VersionedTransaction.deserialize(Buffer.from(b64, "base64"));
    tx.sign([signer]);
    const sig = await connection.sendRawTransaction(tx.serialize(), { skipPreflight: false });
    console.log(`  ${label}: ${sig}`);
    await connection.confirmTransaction(sig, "confirmed");
    return sig;
  }

  async function sendLegacy(b64, label) {
    const tx = Transaction.from(Buffer.from(b64, "base64"));
    tx.partialSign(signer);
    const sig = await connection.sendRawTransaction(
      tx.serialize({ requireAllSignatures: false }),
      { skipPreflight: false }
    );
    console.log(`  ${label}: ${sig}`);
    await connection.confirmTransaction(sig, "confirmed");
    return sig;
  }

  const allRouteSigs = [];
  const allSessionSigs = [];
  const allDispersionSigs = [];
  let keeperFundingSignature = undefined;
  let orchestratorInitSignature = undefined;

  for (let attempt = 1; attempt <= 4; attempt++) {
    console.log(`\n[Attempt ${attempt}/4] Preparing transfer #${transferId}...`);
    let preparedTxs;
    try {
      const resp = await mhRequest("POST", `/transfers/${transferId}/prepare`);
      preparedTxs = resp.preparedTxs;
    } catch (err) {
      if (err.code === "MH_040") {
        console.error("MH_040: Fee tier shifted since quote. Re-quote via POST /api/batches/<batchId>/transfers/" + transferId + "/requote.");
      } else if (err.code === "MH_091") {
        console.error("MH_091: Ensure your registered MultiHopper rewards wallet holds >= rent-exempt minimum SOL.");
      }
      throw err;
    }

    if (preparedTxs.resume?.nothingToDo) {
      console.log(`Transfer ${transferId}: all on-chain accounts already initialized.`);
      const current = await mhRequest("GET", `/transfers/${transferId}`);
      if (["awaiting_signature", "funding_confirmed"].includes(current.status) && allRouteSigs.length) {
        const finalPayload = { routeInitSignatures: allRouteSigs };
        if (keeperFundingSignature) finalPayload.keeperFundingSignature = keeperFundingSignature;
        if (orchestratorInitSignature) finalPayload.orchestratorInitSignature = orchestratorInitSignature;
        if (allSessionSigs.length) finalPayload.sessionInitSignatures = allSessionSigs;
        if (allDispersionSigs.length) finalPayload.dispersionInitSignatures = allDispersionSigs;
        const confirmed = await mhRequest("POST", `/transfers/${transferId}/confirm-broadcast`, finalPayload);
        console.log("Finalized confirm-broadcast:", JSON.stringify(confirmed));
      } else {
        console.log(`Current status: ${current.status} (phase=${current.phase || "-"})`);
      }
      if (poll) {
        return await pollUntilSettled(transferId);
      }
      return current;
    }

    try {
      // Step 1: Broadcast keeperFundingTx FIRST and report immediately
      if (preparedTxs.keeperFundingTx) {
        keeperFundingSignature = await sendVersioned(preparedTxs.keeperFundingTx, "1/5 keeperFundingTx");
        await mhRequest("POST", `/transfers/${transferId}/confirm-broadcast`, {
          routeInitSignatures: [],
          keeperFundingSignature,
        });
        console.log("  -> Intermediate confirm-broadcast recorded.");
      }

      // Step 2: routeInitTxs (sequential + 3s propagation wait)
      const routeItems = preparedTxs.routeInitTxs || [];
      for (let i = 0; i < routeItems.length; i++) {
        const b64 = typeof routeItems[i] === "string" ? routeItems[i] : routeItems[i].base64;
        allRouteSigs.push(await sendVersioned(b64, `2/5 routeInitTxs[${i}]`));
        if (i < routeItems.length - 1) await sleep(3000);
      }
      if (routeItems.length) await sleep(3000);

      // Step 3: orchestratorInitTx (legacy Transaction + 3s propagation wait)
      if (preparedTxs.orchestratorInitTx) {
        orchestratorInitSignature = await sendLegacy(
          preparedTxs.orchestratorInitTx,
          "3/5 orchestratorInitTx"
        );
        await sleep(3000);
      }

      // Step 4: sessionInitTxs
      for (let i = 0; i < (preparedTxs.sessionInitTxs || []).length; i++) {
        allSessionSigs.push(await sendVersioned(preparedTxs.sessionInitTxs[i], `4/5 sessionInitTxs[${i}]`));
      }

      // Step 5: dispersionInitTxs (Multi-destination only)
      for (let i = 0; i < (preparedTxs.dispersionInitTxs || []).length; i++) {
        allDispersionSigs.push(
          await sendVersioned(preparedTxs.dispersionInitTxs[i], `5/5 dispersionInitTxs[${i}]`)
        );
      }

      // Step 6: Final confirm-broadcast (resumes on MH_041)
      const finalPayload = { routeInitSignatures: allRouteSigs };
      if (keeperFundingSignature) finalPayload.keeperFundingSignature = keeperFundingSignature;
      if (orchestratorInitSignature) finalPayload.orchestratorInitSignature = orchestratorInitSignature;
      if (allSessionSigs.length) finalPayload.sessionInitSignatures = allSessionSigs;
      if (allDispersionSigs.length) finalPayload.dispersionInitSignatures = allDispersionSigs;

      const result = await mhRequest("POST", `/transfers/${transferId}/confirm-broadcast`, finalPayload);
      console.log("Confirmed! Transfer advanced:", JSON.stringify(result));

      if (poll) {
        await pollUntilSettled(transferId);
      }
      return result;
    } catch (err) {
      const msg = String(err.message || "");
      const retryable =
        err.code === "MH_041" ||
        msg.includes("Blockhash not found") ||
        msg.includes("block height exceeded") ||
        msg.includes("TransactionExpiredBlockheightExceededError");
      if (retryable && attempt < 4) {
        console.warn(`Retryable error (${err.code || msg}); resuming via fresh /prepare...`);
        await sleep(2000);
        continue;
      }
      throw err;
    }
  }
}

async function pollUntilSettled(transferId, maxPolls = 60, intervalMs = 5000) {
  console.log(`\nPolling GET /transfers/${transferId} until terminal state...`);
  for (let i = 0; i < maxPolls; i++) {
    const tr = await mhRequest("GET", `/transfers/${transferId}`);
    const hops = tr.progress ? `${tr.progress.hopsCompleted}/${tr.progress.hopsTotal}` : "-";
    console.log(`  [${i + 1}] status=${tr.status} phase=${tr.phase || "-"} hops=${hops}`);
    if (["completed", "failed", "expired", "refunded"].includes(tr.status)) {
      return tr;
    }
    await sleep(intervalMs);
  }
  console.log("Polling window reached; check transfer status later.");
  return null;
}

if (process.argv[1]?.endsWith("broadcast_ordered.mjs") && process.argv[2]) {
  const tid = Number(process.argv[2]);
  const poll = process.argv.includes("--poll");
  const kp = process.env.SOLANA_KEYPAIR_PATH;
  if (!kp) {
    console.error("Set SOLANA_KEYPAIR_PATH=/path/to/keypair.json");
    process.exit(1);
  }
  signAndBroadcastTransfer(tid, kp, { poll }).catch((e) => {
    console.error("Broadcast failed:", e.code || "", e.message);
    process.exit(1);
  });
}
