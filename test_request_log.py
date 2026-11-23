import base64
import importlib.util
import io
import json

spec = importlib.util.spec_from_file_location("pg_live", "/opt/data/serve/paidgate.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

TMP = "/opt/data/cache/scratch/pg_hits_fixture.jsonl"
open(TMP, "w").close()
pg.HITS = TMP

FIELDS = {"ts", "path", "method", "ua", "paid_or_402", "ip_class", "event", "detail"}


class H:
    def __init__(self, headers=None, ip="10.244.0.44"):
        base = {"Host": "seed-0x01.konnektr.io", "User-Agent": "fixture/1.0"}
        base.update(headers or {})
        self.headers = base
        self.client_address = (ip, 12345)
        self.wfile = io.BytesIO()
        self.code = None
        self.hdrs = {}

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


def lines():
    return [json.loads(x) for x in open(TMP).read().splitlines() if x.strip()]


def clear():
    open(TMP, "w").close()


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


accepted = {"scheme": "exact", "network": pg.NETWORK, "asset": pg.USDC,
            "payTo": pg.PAY_TO, "amount": pg.AMOUNT, "maxTimeoutSeconds": 300}
good = b64({"x402Version": 2, "accepted": accepted,
            "payload": {"authorization": {"from": "0x1111111111111111111111111111111111111111"}}})

results = []


def check(label, cond, extra=""):
    results.append((label, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + label + (("  " + str(extra)) if extra else ""))


# 1. non-paid path writes nothing
clear()
h = H()
check("non-paid path -> False, 0 lines", pg.handle(h, "GET", "/") is False and len(lines()) == 0)

# 2. wrong method
clear()
h = H()
pg.handle(h, "POST", "/paid/swapcheck?x=1")
ls = lines()
check("405: exactly 1 line", h.code == 405 and len(ls) == 1, (h.code, len(ls)))
if ls:
    r = ls[0]
    check("405: five fields + no raw IP", FIELDS <= set(r) and "10.244.0.44" not in json.dumps(r), r)
    check("405: path stripped of query", r["path"] == "/paid/swapcheck", r["path"])
    check("405: private IP classed", r["ip_class"] == "private", r["ip_class"])
    check("405: paid_or_402=402", r["paid_or_402"] == "402", r["paid_or_402"])

# 3. plain 402
clear()
h = H(ip="127.0.0.1")
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("402: exactly 1 line", h.code == 402 and len(ls) == 1, (h.code, len(ls)))
if ls:
    r = ls[0]
    check("402: loopback classed", r["ip_class"] == "loopback", r["ip_class"])
    check("402: ua + method bound", r["ua"] == "fixture/1.0" and r["method"] == "GET", r)

# 4. oversized header
clear()
h = H({"PAYMENT-SIGNATURE": "A" * 9000})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("oversized header: 1 line, event/detail", len(ls) == 1 and ls[0]["event"] == "402"
      and ls[0]["detail"] == "payment header too large" and ls[0]["paid_or_402"] == "paid", ls)

# 5. malformed base64
clear()
h = H({"PAYMENT-SIGNATURE": "!!!!not-b64!!!!"})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("malformed b64: 1 line", len(ls) == 1 and ls[0]["detail"] == "malformed base64 payment header", ls)

# 6. wrong version
clear()
h = H({"PAYMENT-SIGNATURE": b64({"x402Version": 1})})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("wrong version: 1 line", len(ls) == 1 and "x402 version" in ls[0]["detail"], ls)

# 7. terms rejected
clear()
h = H({"PAYMENT-SIGNATURE": b64({"x402Version": 2, "accepted": {"amount": "1"}})})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("terms rejected: 1 line", len(ls) == 1 and ls[0]["event"] == "attempt_rejected", ls)

# 8-11: monkeypatch facilitator + report
orig_post, orig_report = pg.post, pg.fresh_report


def mk_post(verify, settle):
    def f(path, body, timeout=25):
        return verify if path == "/verify" else settle
    return f


# verify failed
clear()
pg.post = mk_post(({"isValid": False, "invalidReason": "bad sig"}, 200), ({"success": True}, 200))
h = H({"PAYMENT-SIGNATURE": good})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("verify failed: 1 line", h.code == 402 and len(ls) == 1 and ls[0]["event"] == "402_verify_failed", ls)

# report generation failed
clear()
pg.post = mk_post(({"isValid": True}, 200), ({"success": True}, 200))
pg.fresh_report = lambda: (_ for _ in ()).throw(RuntimeError("quote down"))
h = H({"PAYMENT-SIGNATURE": good})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("report failed: 1 line", h.code == 502 and len(ls) == 1 and ls[0]["event"] == "502_report", ls)

# settle failed
clear()
pg.post = mk_post(({"isValid": True}, 200), ({"success": False, "error": "insufficient funds"}, 200))
pg.fresh_report = lambda: {"payload": {"generated_at": "2026-11-23T00:00:00Z", "signer": "0xabc"},
                           "signature": "sig"}
h = H({"PAYMENT-SIGNATURE": good})
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("settle failed: 1 line", h.code == 502 and len(ls) == 1 and ls[0]["event"] == "502_settle", ls)

# settled 200
clear()
pg.post = mk_post(({"isValid": True}, 200), ({"success": True, "transaction": "0xdeadbeef"}, 200))
h = H({"PAYMENT-SIGNATURE": good, "User-Agent": "outside-buyer/1.0"}, ip="8.8.8.8")
pg.handle(h, "GET", "/paid/swapcheck")
ls = lines()
check("settled: exactly 1 line", h.code == 200 and len(ls) == 1, (h.code, len(ls)))
if ls:
    r = ls[0]
    check("settled: public IP classed", r["ip_class"] == "public", r["ip_class"])
    check("settled: event 200 + paid", r["event"] == "200" and r["paid_or_402"] == "paid", r)
    check("settled: no raw IP leaked", "8.8.8.8" not in json.dumps(r), r)

pg.post, pg.fresh_report = orig_post, orig_report
print("\nTOTAL %d/%d PASS" % (sum(1 for _, ok, _ in results if ok), len(results)))
if not all(ok for _, ok, _ in results):
    raise SystemExit(1)
