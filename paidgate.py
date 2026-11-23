"""paidgate — x402 v2 paywalled resource server module.

Served by routecheck.py at GET /paid/swapcheck. This module NEVER signs a
payment and never sees a secret in an HTTP request. It:

  1. returns a protocol-correct 402 with a base64 PAYMENT-REQUIRED header
     (x402 v2 PaymentRequired, PayAI `exact` on Base / eip155:8453),
  2. on PAYMENT-SIGNATURE, enforces my own terms (payTo / amount / asset /
     network), then calls the third-party facilitator /verify and /settle,
  3. on a settled payment, releases a FRESHLY generated, EIP-191-signed
     swapcheck report and logs a settlement receipt (never a key).

The resource is read-only market data. No trade is executed, no key material
leaves the pod, and no arbitrary data is signed: the report schema and pairs
are fixed in this module.

Runtime deps: stdlib + /opt/data/bin/treasury.py (already on the server's
PYTHONPATH). The x402 SDK / eth-account are used only by test.py, never here.
"""
import base64
import datetime
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request

FACILITATOR = "https://facilitator.payai.network"
NETWORK = "eip155:8453"
LEGACY_NETWORK = "base"
CHAIN = "base"
CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # native USDC, Base
PAY_TO = "0x663952620CEeF96C9Ebe9B5002Cc5910A7CD8FFd"
AMOUNT = "50000"            # $0.05 USDC (6 decimals) — introductory price for the
                            # first outside buyer. Why not $1.00: the only machine
                            # buyer channel that exists (agent402's router) caps an
                            # UNPROVEN seller at $0.05/call on Base, so a $1 price is
                            # reachable only by a direct buyer I do not have yet. The
                            # price rises to $1.00 after the first settled payment
                            # from a payer who is not me — stated here, not implied.
MAX_TIMEOUT = 300           # seconds; the offer's validity window
MAX_HEADER = 8192           # refuse oversized PAYMENT-SIGNATURE headers
RESOURCE_PATH = "/paid/swapcheck"
LEDGER = "/opt/data/projects/paidgate/settlements.jsonl"
HITS = "/opt/data/projects/paidgate/hits.jsonl"
TOKEN_NAME = "USD Coin"
TOKEN_VERSION = "2"
DESCRIPTION = ("Fresh signed swap-sanity report: live Uniswap V3 quotes on Base, "
               "every route ranked after gas, EIP-191 signed by Tallow's wallet. "
               "Read-only; no trade executed. Autonomous AI agent service. "
               "Introductory price: 0.05 USDC, rising to 1.00 USDC after the first "
               "settled payment from an outside payer.")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def b64e(obj):
    raw = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def b64d(text):
    raw = base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    return json.loads(raw)


def _bazaar():
    """Self-description carried in the 402 so a Bazaar-conformant crawler can
    list this route from the challenge alone (shape: docs.x402.org/extensions/bazaar).

    Without this block a directory sees a 402 with a price but no idea what the
    endpoint returns, so no buyer is ever routed to it. The example below is the
    envelope's real shape (swapcheck-envelope/1), not a plausible invention.
    """
    return {
        "bazaar": {
            "info": {
                "input": {"type": "http", "method": "GET"},
                "output": {
                    "type": "json",
                    "example": {
                        "schema": "swapcheck-envelope/1",
                        "payload": {
                            "schema": "swapcheck/1",
                            "chain": "base",
                            "pairs": [{
                                "asset": "WETH", "target": "USDC",
                                "routes": [{"fee": "500", "net": "…"}],
                            }],
                        },
                        "signature": "0x…",
                        "signed_by": PAY_TO,
                    },
                },
            }
        }
    }


def offer(host=None):
    """The x402 v2 PaymentRequired object this resource accepts.

    Field names are the SDK's camelCase aliases (payTo, maxTimeoutSeconds,
    x402Version, mimeType), serialized by_alias on the wire.
    """
    url = "https://%s%s" % (host or "seed-0x01.konnektr.io", RESOURCE_PATH)
    return {
        "x402Version": 2,
        "resource": {
            "url": url,
            "description": DESCRIPTION,
            "mimeType": "application/json",
            "serviceName": "Tallow swapcheck",
        },
        "accepts": [{
            "scheme": "exact",
            "network": NETWORK,
            "asset": USDC,
            "amount": AMOUNT,
            "payTo": PAY_TO,
            "maxTimeoutSeconds": MAX_TIMEOUT,
            "extra": {"name": TOKEN_NAME, "version": TOKEN_VERSION},
        }],
        "extensions": _bazaar(),
    }


def terms_ok(accepted):
    """Enforce MY terms on the requirement the payer claims to fulfil.

    Re-checking these locally is what stops a payer from settling 0.000001
    USDC to someone else and still getting the report. The facilitator also
    checks them, but a local check gives a clean 402 instead of a 5xx and
    makes the served terms independent of facilitator behaviour.
    """
    if not isinstance(accepted, dict):
        return False, "missing accepted requirements"
    expected = {
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC.lower(),
        "payTo": PAY_TO.lower(),
        "amount": AMOUNT,
    }
    actual = {
        "scheme": accepted.get("scheme"),
        "network": accepted.get("network"),
        "asset": str(accepted.get("asset", "")).lower(),
        "payTo": str(accepted.get("payTo", "")).lower(),
        "amount": str(accepted.get("amount")),
    }
    bad = [k for k in expected if actual[k] != expected[k]]
    if bad:
        return False, "terms mismatch: " + ", ".join(sorted(bad))
    try:
        if int(accepted.get("maxTimeoutSeconds", 0)) > MAX_TIMEOUT:
            return False, "maxTimeoutSeconds too long"
    except (TypeError, ValueError):
        return False, "bad maxTimeoutSeconds"
    return True, "ok"


def post(path, body, timeout=25):
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        FACILITATOR + path, data=raw,
        headers={"Content-Type": "application/json", "User-Agent": "tallow-paidgate/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode("utf-8", "replace")[:500]}, e.code
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__ + ": " + str(e)[:200]}, -1


def fresh_report():
    """Generate a NEW signed swapcheck envelope (not the frozen demo file).

    Uses the same canonical JSON + personal_sign scheme as swapcheck/build.py,
    loaded lazily so there is no import cycle with routecheck.py at startup.
    Called BEFORE /settle: if quote generation fails, the payer is not charged.
    """
    import importlib.util
    if "/opt/data/bin" not in sys.path:
        sys.path.insert(0, "/opt/data/bin")
    import treasury as T  # noqa: E402

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # swapcheck/build.py does `import routecheck as R` expecting the LIBRARY
    # (/opt/data/bin), but this process runs the SERVER (/opt/data/serve) as
    # __main__ and serves another copy as `routecheck`. Point that name at the
    # library for the duration, or collect_pair dies on missing quote_best.
    saved = sys.modules.get("routecheck")
    sys.modules["routecheck"] = _load("routecheck_lib", "/opt/data/bin/routecheck.py")
    try:
        sb = _load("swapcheck_build", "/opt/data/projects/swapcheck/build.py")
        mnem = T.load_key()
        priv = T.private_key_bytes(mnem)
        signer = T.derive_address(mnem)
        pairs = [sb.collect_pair(CHAIN, sym, amt) for sym, amt in sb.PAIRS]
    finally:
        if saved is None:
            sys.modules.pop("routecheck", None)
        else:
            sys.modules["routecheck"] = saved
    blk = T.rpc(CHAIN, "eth_getBlockByNumber", ["latest", False])
    payload = {
        "schema": "swapcheck/1",
        "generated_at": now_iso(),
        "chain": CHAIN, "chain_id": CHAIN_ID,
        "signer": signer,
        "block": {"number": int(blk["number"], 16),
                  "base_fee_wei": int(blk["baseFeePerGas"], 16)},
        "quote_source": "Uniswap V3 QuoterV2 via public RPC (free eth_call)",
        "pairs": pairs,
        "disclosure": sb.DISCLOSE,
        "served_by": "paidgate (x402 v2, settled before release)",
    }
    digest = sb.personal_digest(sb.canonical_bytes(payload))
    sig = bytes(T.PrivateKey(priv).sign_recoverable(digest, hasher=None))
    signature = "0x" + sig[:64].hex() + format(sig[64], "02x")
    return {
        "schema": "swapcheck-envelope/1",
        "scheme": "EIP-191 personal_sign / secp256k1 over canonical JSON (sort_keys, compact)",
        "signature": signature,
        "payload": payload,
    }


def log_settlement(rec):
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - logging must never fail a settled request
        pass


def settlement_count():
    try:
        with open(LEDGER) as f:
            return sum(1 for ln in f if ln.strip())
    except FileNotFoundError:
        return 0


def _send(handler, code, body, extra=None):
    raw = json.dumps(body, indent=2).encode("utf-8") if not isinstance(body, (bytes, str)) else (
        body.encode("utf-8") if isinstance(body, str) else body)
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Expose-Headers", "PAYMENT-REQUIRED, PAYMENT-RESPONSE")
    handler.send_header("Cache-Control", "no-store")
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(raw)


def _ip_class(ip):
    """Coarse client IP class only — never persist a raw address in telemetry."""
    try:
        a = ipaddress.ip_address(str(ip))
        if a.is_loopback:
            return "loopback"
        if a.is_private:
            return "private"
        if a.is_link_local:
            return "link-local"
        if a.is_reserved or a.is_multicast or a.is_unspecified:
            return "special"
        return "public"
    except Exception:  # noqa: BLE001 - a bad address is still a countable hit
        return "unknown"


def _hit(handler, event, detail=""):
    """Emit exactly ONE telemetry line per paid-route request.

    Landlord decision 2026-11-23: every paid endpoint reports UTC, path, UA,
    paid-or-402 and client IP class, so "nobody came" becomes a countable
    deletion instead of a guess. No raw IP, no body, no key. Telemetry must
    never break the sale, hence the blanket guard.
    """
    try:
        raw = handler.headers.get("PAYMENT-SIGNATURE") or handler.headers.get("X-PAYMENT")
        rec = {
            "ts": now_iso(),
            "path": getattr(handler, "_pg_path", RESOURCE_PATH),
            "method": getattr(handler, "_pg_method", "GET"),
            "ua": str(handler.headers.get("User-Agent", ""))[:120],
            "paid_or_402": "paid" if raw else "402",
            "ip_class": _ip_class(handler.client_address[0] if handler.client_address else ""),
            "event": event,
            "detail": str(detail)[:200],
        }
        with open(HITS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never break the sale
        pass


def handle(handler, method, path):
    """Return True if this request was for the paid resource and was answered."""
    if path.split("?", 1)[0] != RESOURCE_PATH:
        return False
    handler._pg_path = path.split("?", 1)[0]
    handler._pg_method = method
    if method != "GET":
        _hit(handler, "405", "method not allowed")
        _send(handler, 405, {"error": "GET only"})
        return True

    host = handler.headers.get("Host", "").split(",")[0].strip()
    pr = offer(host)
    raw = handler.headers.get("PAYMENT-SIGNATURE") or handler.headers.get("X-PAYMENT")

    if not raw:
        # No payment: a standards-shaped 402. Body is a human-readable copy;
        # the base64 PAYMENT-REQUIRED header is what an x402 client parses.
        _hit(handler, "402", "no payment header")
        _send(handler, 402,
              {"x402Message": "Payment required. See PAYMENT-REQUIRED header.",
               "offer": pr},
              {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    if len(raw) > MAX_HEADER:
        _hit(handler, "402", "payment header too large")
        _send(handler, 402, {"error": "payment header too large"},
              {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    try:
        payload = b64d(raw)
    except Exception:  # noqa: BLE001
        _hit(handler, "402", "malformed base64 payment header")
        _send(handler, 402,
              {"error": "malformed base64 payment header"},
              {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    version = payload.get("x402Version")
    if version != 2:
        _hit(handler, "402", "x402 version %r" % version)
        _send(handler, 402,
              {"error": "x402 v2 required (got %r); v1 legacy is not accepted" % version},
              {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    ok, why = terms_ok(payload.get("accepted"))
    if not ok:
        _hit(handler, "attempt_rejected", why)
        _send(handler, 402, {"error": why}, {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    requirement = pr["accepts"][0]
    verify_body = {
        "x402Version": 2,
        "paymentPayload": payload,
        "paymentRequirements": requirement,
    }
    vres, vcode = post("/verify", verify_body)
    if not vres.get("isValid"):
        _hit(handler, "402_verify_failed",
             str(vres.get("invalidReason") or vres.get("error") or "")[:80])
        _send(handler, 402,
              {"error": "payment not verified by facilitator",
               "reason": vres.get("invalidReason") or vres.get("error") or vres,
               "facilitator_http": vcode},
              {"PAYMENT-REQUIRED": b64e(pr)})
        return True

    # Generate BEFORE settling: a quote failure must not charge the payer.
    try:
        report = fresh_report()
    except Exception as e:  # noqa: BLE001
        _hit(handler, "502_report", type(e).__name__ + ": " + str(e)[:80])
        _send(handler, 502, {"error": "report generation failed; payment NOT settled",
                             "detail": type(e).__name__ + ": " + str(e)[:200]})
        return True

    sres, scode = post("/settle", verify_body)
    if not sres.get("success"):
        _hit(handler, "502_settle", str(sres.get("error") or "")[:80])
        _send(handler, 502,
              {"error": "facilitator settle failed",
               "reason": sres.get("error") or sres,
               "facilitator_http": scode})
        return True

    payer = (sres.get("payer") or
             (payload.get("payload", {}).get("authorization", {}) or {}).get("from"))
    receipt = {
        "ts": now_iso(),
        "txHash": sres.get("transaction") or sres.get("txHash"),
        "network": NETWORK,
        "amount": AMOUNT,
        "asset": USDC,
        "payTo": PAY_TO,
        "payer": payer,
        "resource": url_for(host),
        "report_generated_at": report["payload"]["generated_at"],
        "report_signer": report["payload"]["signer"],
        "report_signature": report["signature"],
    }
    log_settlement(receipt)
    _hit(handler, "200", "settled " + str(receipt.get("txHash", ""))[:60])
    _send(handler, 200,
          {"schema": "paidgate-receipt/1", "receipt": receipt, "resource": report},
          {"PAYMENT-RESPONSE": b64e(sres)})
    return True


def url_for(host=None):
    return "https://%s%s" % (host or "seed-0x01.konnektr.io", RESOURCE_PATH)
