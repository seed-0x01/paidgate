#!/usr/bin/env python3
"""Register /paid/swapcheck on x402scan via its SIWX-authed registry API.

SIWX (Sign-In With X) = one wallet signature (EIP-4361 / EIP-191), NOT a payment.
Source: https://www.x402scan.com/resources/register
Flow: POST url -> 402 with sign-in-with-x challenge -> sign CAIP-122 message with
our payTo wallet -> resend base64 payload in SIGN-IN-WITH-X header. No funds move.
Run with /usr/local/bin/python3 (has coincurve, pycryptodome, bip_utils).
"""
import base64, json, sys, urllib.request, urllib.error
from coincurve import PrivateKey
from Crypto.Hash import keccak
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes

import os
ORIGIN = "https://seed-0x01.konnektr.io"
RESOURCE = ORIGIN + "/paid/swapcheck"
MODE = os.environ.get("X402_MODE", "single")  # single | origin
REGISTER = ("https://www.x402scan.com/api/x402/registry/register-origin"
            if MODE == "origin"
            else "https://www.x402scan.com/api/x402/registry/register")
BODY = {"origin": ORIGIN} if MODE == "origin" else {"url": RESOURCE}
UA = "tallow-x402-scan-registrar/1.0 (autonomous agent; seed-0x01)"


def keccak256(b):
    k = keccak.new(digest_bits=256)
    k.update(b)
    return k.digest()


def load_key():
    mnem = open("/opt/data/secrets/wallet.mnemonic").read().strip()
    seed = Bip39SeedGenerator(mnem).Generate()
    acct = (Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
            .Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
    return acct.PrivateKey().Raw().ToBytes()


def checksum_address(priv):
    raw = PrivateKey(priv).public_key.format(compressed=False)[1:]
    low = keccak256(raw)[-20:].hex()
    h = keccak256(low.encode()).hex()
    return "0x" + "".join(c.upper() if c in "abcdef" and int(h[i], 16) >= 8 else c
                          for i, c in enumerate(low))


def personal_sign(priv, msg):
    prefixed = b"\x19Ethereum Signed Message:\n" + str(len(msg.encode())).encode() + msg.encode()
    sig = bytes(PrivateKey(priv).sign_recoverable(keccak256(prefixed), hasher=None))
    return "0x" + sig[:64].hex() + format(sig[64] + 27, "02x")


def siwe_message(info, address):
    cid = int(info["chainId"].split(":")[1])
    lines = [
        f'{info["domain"]} wants you to sign in with your Ethereum account:',
        address,
        "",
        info.get("statement", ""),
        "",
        f'URI: {info["uri"]}',
        f'Version: {info["version"]}',
        f"Chain ID: {cid}",
        f'Nonce: {info["nonce"]}',
        f'Issued At: {info["issuedAt"]}',
    ]
    if info.get("expirationTime"):
        lines.append(f'Expiration Time: {info["expirationTime"]}')
    if info.get("notBefore"):
        lines.append(f'Not Before: {info["notBefore"]}')
    return "\n".join(lines)


def post(body, header=None):
    headers = {"content-type": "application/json", "User-Agent": UA}
    if header:
        headers["SIGN-IN-WITH-X"] = header
    req = urllib.request.Request(REGISTER, data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=45)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    priv = load_key()
    address = checksum_address(priv)
    body = BODY
    print("signer:", address)
    print("mode:", MODE, "endpoint:", REGISTER)
    print("body:", body)

    st, txt = post(body)
    print("step1 status:", st)
    if st != 402:
        print("step1 body:", txt[:1000])
        print("VERDICT: unexpected (not a 402 challenge); nothing signed")
        return
    challenge = json.loads(txt)
    ext = challenge.get("extensions", {}).get("sign-in-with-x", {})
    info = ext.get("info") or ext
    if not info or "nonce" not in info:
        print("VERDICT: no sign-in-with-x challenge info; nothing signed")
        return
    print("challenge: domain=%s nonce=%s chain=%s exp=%s" % (
        info.get("domain"), info["nonce"], info.get("chainId"), info.get("expirationTime")))

    msg = siwe_message(info, address)
    sig = personal_sign(priv, msg)
    payload = {
        "domain": info["domain"], "address": address,
        "statement": info.get("statement", ""), "uri": info["uri"],
        "version": info["version"], "chainId": info["chainId"],
        "type": info.get("type", "eip191"), "nonce": info["nonce"],
        "issuedAt": info["issuedAt"],
    }
    for k in ("expirationTime", "notBefore", "requestId", "resources"):
        if info.get(k):
            payload[k] = info[k]
    payload["signature"] = sig
    header = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    print("signed EIP-191 signature:", sig[:20] + "..." + sig[-6:])

    st2, txt2 = post(body, header)
    print("step2 status:", st2)
    print("step2 body:", txt2[:2000])
    print("VERDICT:", "SUBMITTED" if st2 in (200, 201, 202) else "NOT SUBMITTED")


if __name__ == "__main__":
    main()
