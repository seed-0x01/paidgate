#!/usr/local/bin/python3
"""paidgate test: pure fixtures + real integration against the served router.

Run: /usr/local/bin/python3 /opt/data/projects/paidgate/test.py [--settle]

Without --settle: 12 offline/live-shape fixtures + a real 402 offer fetched
from an in-process instance of the actual routecheck Handler.

With --settle: additionally build a REAL EIP-3009 authorization from my own
key (x402 SDK + eth_account), send it through the served handler and let PayAI
verify+settle. from == payTo == my wallet, so this is a self-test: net value
transfer is zero, no gas is paid by me, and it is NOT revenue. It proves the
wire path end to end. A buyer paying is the separate revenue falsifier.
"""
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SP = "/opt/data/home/.local/lib/python3.14/site-packages"
for p in (HERE, "/opt/data/bin", SP, "/opt/data/serve"):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)  # last inserted wins -> /opt/data/serve routecheck.py (has Handler)

import paidgate  # noqa: E402
import routecheck  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" :: " + str(detail)[:300] if detail else ""))


# ---- pure fixtures ---------------------------------------------------------
def fixtures():
    pr = paidgate.offer("example.test")
    check("offer.x402Version==2", pr["x402Version"] == 2)
    a = pr["accepts"][0]
    check("offer scheme/network", a["scheme"] == "exact" and a["network"] == "eip155:8453")
    check("offer amount==1 USDC atomic", a["amount"] == "1000000")
    check("offer payTo is my wallet", a["payTo"].lower() == paidgate.PAY_TO.lower())
    check("offer asset is native Base USDC", a["asset"].lower() == paidgate.USDC.lower())
    check("offer has EIP-712 domain extra", a.get("extra", {}).get("name") == "USD Coin")

    rt = paidgate.b64d(paidgate.b64e(pr))
    check("base64 offer round-trips", rt == pr)

    check("terms_ok accepts honest payer", paidgate.terms_ok(a) == (True, "ok"))
    for field, value in (("amount", "1"), ("payTo", "0x" + "0" * 40),
                         ("asset", "0x" + "1" * 40), ("network", "base"),
                         ("scheme", "permit2")):
        bad = dict(a); bad[field] = value
        check("terms_ok rejects tampered " + field,
              paidgate.terms_ok(bad)[0] is False, paidgate.terms_ok(bad))
    check("terms_ok rejects missing accepted", paidgate.terms_ok(None)[0] is False)


# ---- live facilitator shape ------------------------------------------------
def facilitator_supported():
    try:
        with urllib.request.urlopen("https://facilitator.payai.network/supported", timeout=20) as r:
            data = json.load(r)
        kinds = data.get("kinds", [])
        hit = any(k.get("scheme") == "exact" and k.get("network") == "eip155:8453"
                  and k.get("x402Version") == 2 for k in kinds)
        check("PayAI supports exact/eip155:8453 v2", hit, "%d kinds" % len(kinds))
    except Exception as e:  # noqa: BLE001
        check("PayAI /supported reachable", False, e)


# ---- served handler --------------------------------------------------------
def start_server():
    srv = urllib.request  # keep import referenced; real server below
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), routecheck.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def http_get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def served_offer(base):
    code, headers, body = http_get(base, paidgate.RESOURCE_PATH)
    check("GET /paid/swapcheck without pay -> 402", code == 402, code)
    hdr = headers.get("PAYMENT-REQUIRED")
    check("402 carries PAYMENT-REQUIRED header", bool(hdr))
    if hdr:
        decoded = paidgate.b64d(hdr)
        check("served header parses as PaymentRequired",
              decoded.get("x402Version") == 2 and len(decoded.get("accepts", [])) == 1)
    check("CORS exposes PAYMENT-REQUIRED",
          "PAYMENT-REQUIRED" in headers.get("Access-Control-Expose-Headers", ""))
    return headers


def settle_self_test(base):
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.schemas import PaymentRequired
    from eth_account import Account
    import treasury as T

    priv = "0x" + T.private_key_bytes(T.load_key()).hex()
    acct = Account.from_key(priv)
    check("test signer is my wallet", acct.address.lower() == paidgate.PAY_TO.lower(),
          acct.address)

    pr = paidgate.offer("127.0.0.1")
    client = x402ClientSync()
    client.register("eip155:8453", ExactEvmScheme(signer=acct))
    payload = client.create_payment_payload(PaymentRequired(**pr))
    # SDK returns a PaymentPayload model; serialize with its own camelCase
    # aliases exactly as encode_payment_signature_header() does.
    hdr = paidgate.b64e(payload.model_dump_json(by_alias=True, exclude_none=True))
    code, headers, body = http_get(base, paidgate.RESOURCE_PATH,
                                   {"PAYMENT-SIGNATURE": hdr})
    text = body.decode("utf-8", "replace")
    check("settled request -> 200", code == 200, "code=%d body=%s" % (code, text[:500]))
    if code == 200:
        env = json.loads(text)
        with open(os.path.join(HERE, "last_settlement.json"), "w") as f:
            f.write(text)
        check("receipt has tx hash", bool(env["receipt"].get("txHash")), env["receipt"].get("txHash"))
        check("fresh report signer is my wallet",
              env["resource"]["payload"]["signer"].lower() == paidgate.PAY_TO.lower())
        check("PAYMENT-RESPONSE header present", bool(headers.get("PAYMENT-RESPONSE")))
        print("SELF-SETTLEMENT TX:", env["receipt"].get("txHash"))

    # tampered amount must be refused before any facilitator call
    bad = json.loads(base64.b64decode(hdr))
    bad["accepted"]["amount"] = "1"
    code2, _, body2 = http_get(base, paidgate.RESOURCE_PATH,
                               {"PAYMENT-SIGNATURE": paidgate.b64e(bad)})
    check("tampered amount -> 402, no report", code2 == 402,
          "code=%d body=%s" % (code2, body2.decode("utf-8", "replace")[:300]))


def main():
    fixtures()
    facilitator_supported()
    httpd, base = start_server()
    try:
        served_offer(base)
        if "--settle" in sys.argv:
            settle_self_test(base)
    finally:
        httpd.shutdown()
    print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    print("settlements logged:", paidgate.settlement_count())
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
