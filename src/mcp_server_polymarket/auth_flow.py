"""`mcp-server-polymarket auth` and `logout`: everything that creates or removes credentials.

Tools only use credentials. Creating them, which can onboard an account or hand a key trading
rights, happens here, in a terminal the user is watching.
"""

import argparse
import json
import secrets
import sys
import time
import uuid
from typing import Any

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import ApiClient, parse_json
from mcp_server_polymarket.client.session import (
    ClobCredentials,
    CredentialStore,
    PerpsSession,
    write_private,
)
from mcp_server_polymarket.config import Settings, config_dir
from mcp_server_polymarket.errors import AuthError, ConfigError, InvalidRequest, Refused
from mcp_server_polymarket.identity import Identity

SESSION_KEY_LIFETIME = 4_315 * 3600  # the lifetime the Relayer accepts, about 180 days
MAX_PERPS_DAYS = 30


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _confirm(question: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    answer = input(f"{question} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise Refused("Cancelled.")


def _context(settings: Settings) -> tuple[Identity, ApiClient, CredentialStore]:
    from mcp_server_polymarket import signing

    signing.require()
    settings.register_secrets()
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    identity = Identity(settings, store)
    if identity.signer_key is None:
        import getpass

        key = getpass.getpass("Private key of the signer (input hidden): ").strip()
        if not key:
            raise ConfigError("No private key given.")
        settings.private_key = key  # type: ignore[assignment]
        identity = Identity(settings, store)
    return identity, ApiClient(settings, identity), store


async def auth(args: argparse.Namespace, settings: Settings) -> int:
    identity, client, store = _context(settings)
    try:
        if args.perps:
            result = await create_perps_session(identity, client, store, args)
        elif args.session_key:
            result = await change_session_key(identity, client, args.session_key, True, args.yes)
        elif args.revoke_session_key:
            result = await change_session_key(
                identity, client, args.revoke_session_key, False, args.yes
            )
        elif args.builder_key:
            result = await create_builder_key(identity, client)
        else:
            result = await clob_credentials(identity, client, store)
    finally:
        await client.aclose()
    print(json.dumps(result, indent=2))
    return 0


async def clob_credentials(
    identity: Identity, client: ApiClient, store: CredentialStore
) -> dict[str, Any]:
    """Derive the signer's existing CLOB API key, or create one, and store it (0600)."""
    from mcp_server_polymarket.signing.clob import l1_headers

    key = identity.signer_key
    assert key is not None
    headers = l1_headers(key, int(time.time()))
    response = await client.request(ep.CLOB_DERIVE_API_KEY, headers=headers, expect=(400, 404))
    body = parse_json(response) if response.status_code == 200 else None
    created = False
    if not (isinstance(body, dict) and body.get("apiKey")):
        headers = l1_headers(key, int(time.time()))
        body = await client.json(ep.CLOB_CREATE_API_KEY, headers=headers)
        created = True
    if not isinstance(body, dict) or not body.get("apiKey"):
        raise AuthError("Polymarket did not return API credentials.")
    address = identity.signer_address or ""
    credentials = ClobCredentials(
        address=address,
        api_key=str(body["apiKey"]),
        secret=str(body["secret"]),
        passphrase=str(body["passphrase"]),
    )
    store.save_clob(credentials)
    return {
        "status": "created" if created else "derived",
        "signer": address,
        "saved_to": str(store.session_file),
    }


async def create_perps_session(
    identity: Identity, client: ApiClient, store: CredentialStore, args: argparse.Namespace
) -> dict[str, Any]:
    """Authorise a new trading key for the signer's Perps account."""
    from mcp_server_polymarket.signing import eip712
    from mcp_server_polymarket.signing.perps import create_proxy_typed_data

    if identity.is_session_key:
        raise ConfigError(
            "A Perps account is owned by an EOA; use the owner's key, not a session key."
        )
    owner = identity.signer_address
    key = identity.signer_key
    assert owner and key
    days = max(1, min(int(args.days), MAX_PERPS_DAYS))
    registered = await client.json(ep.PERPS_REGISTERED, params={"address": owner}) or {}
    if not registered.get("registered"):
        _say(
            f"{owner} has no Perps account yet. Creating a session also opens one, which "
            "cannot be undone."
        )
    mode = "read-only" if args.read_only else "trading"
    _confirm(f"Authorise a {mode} Perps key for {owner} for {days} days?", args.yes)
    proxy_key, proxy = eip712.new_key()
    now = int(time.time() * 1000)
    expiry = now + days * 86_400_000
    salt = secrets.randbits(32)
    signature = eip712.sign(key, create_proxy_typed_data(proxy, expiry, salt, now))
    label = "read-only:mcp-server-polymarket" if args.read_only else "mcp-server-polymarket"
    body = {
        "op": {"type": "createProxy", "args": {"expiry": expiry, "owner": owner, "proxy": proxy}},
        "salt": salt,
        "sig": signature,
        "ts": now,
        "label": label,
    }
    response = await client.json(ep.PERPS_CREATE_PROXY, body=body) or {}
    secret = response.get("secret")
    if response.get("status") != "ok" or not secret:
        raise AuthError(f"Perps refused the new key: {response.get('error') or response}")
    session = PerpsSession(
        owner=owner,
        proxy=proxy,
        secret=str(secret),
        # A read-only key never needs to sign, so it is not kept.
        proxy_key=None if args.read_only else proxy_key,
        expires_at_ms=expiry,
        label=label,
    )
    store.save_perps(session)
    return {"status": "created", "proxy": proxy, "mode": mode, "saved_to": str(store.perps_file)}


async def change_session_key(
    identity: Identity, client: ApiClient, address: str, authorize: bool, assume_yes: bool
) -> dict[str, Any]:
    """Authorise or revoke a Deposit Wallet session key, through the Relayer."""
    from mcp_server_polymarket import wallet_tx
    from mcp_server_polymarket.identity import checksum
    from mcp_server_polymarket.signing import relayer

    session_key = checksum(address)
    _, _, wallet = wallet_tx.require_deposit_wallet(identity)
    if identity.is_session_key:
        raise ConfigError("Only the wallet owner can change session keys.")
    valid_until = int(time.time()) + SESSION_KEY_LIFETIME
    if authorize:
        _confirm(
            f"Let {session_key} trade from {wallet} for 180 days? It cannot withdraw.",
            assume_yes,
        )
        call = relayer.authorize_session_signer(wallet, session_key, valid_until)
    else:
        _confirm(f"Revoke session key {session_key} on {wallet}?", assume_yes)
        call = relayer.revoke_session_signer(wallet, session_key)
    signed = await wallet_tx.sign_batch(client, identity, [call])
    body: dict[str, Any] = {
        "deadline": str(signed["deadline"]),
        "nonce": str(signed["nonce"]),
        "sessionSignerAddress": session_key,
        "signature": signed["signature"],
        "walletAddress": wallet,
    }
    if authorize:
        body |= {"scopes": ["ALL"], "validUntil": str(valid_until)}
    endpoint = ep.RELAYER_SESSION_AUTHORIZE if authorize else ep.RELAYER_SESSION_REVOKE
    response = await client.json(
        endpoint, body=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    return {"status": "submitted", "session_key": session_key, "response": response}


async def create_builder_key(identity: Identity, client: ApiClient) -> dict[str, Any]:
    if identity.clob_credentials() is None:
        raise ConfigError("Run `mcp-server-polymarket auth` first; builder keys need CLOB auth.")
    body = await client.json(ep.CLOB_CREATE_BUILDER_KEY)
    if not isinstance(body, dict) or not body.get("key") and not body.get("apiKey"):
        raise AuthError("Polymarket did not return a builder key.")
    path = config_dir() / "builder_key.json"
    write_private(path, body)
    return {
        "status": "created",
        "saved_to": str(path),
        "next_step": "Set POLYMARKET_BUILDER_API_KEY, _SECRET and _PASSPHRASE from that file.",
    }


async def logout(args: argparse.Namespace, settings: Settings) -> int:
    settings.register_secrets()
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    if args.perps:
        session = store.load_perps()
        if session is None:
            print(json.dumps({"status": "no Perps session"}))
            return 0
        identity, client, _ = _context(settings)
        try:
            await revoke_perps_session(identity, client, session)
        finally:
            await client.aclose()
        store.forget_perps()
        print(json.dumps({"status": "revoked", "proxy": session.proxy}))
        return 0
    identity = Identity(settings, store)
    address = identity.clob_address
    if address is None:
        raise ConfigError("No signer is configured, so there is nothing to log out.")
    if args.revoke:
        client = ApiClient(settings, identity)
        try:
            await client.json(ep.CLOB_DELETE_API_KEY)
        finally:
            await client.aclose()
    forgotten = store.forget_clob(address)
    print(
        json.dumps(
            {"status": "forgotten" if forgotten else "nothing stored", "revoked": args.revoke}
        )
    )
    return 0


async def revoke_perps_session(
    identity: Identity, client: ApiClient, session: PerpsSession
) -> None:
    from mcp_server_polymarket.signing.perps import delete_proxy, signed_request

    owner_key = identity.signer_key
    if owner_key is None or (identity.signer_address or "").lower() != session.owner.lower():
        raise InvalidRequest("Revoking a Perps key needs the account owner's private key.")
    op = delete_proxy(session.proxy)
    body = signed_request(owner_key, op, salt=secrets.randbits(32), ts_ms=int(time.time() * 1000))
    response = await client.json(ep.PERPS_DELETE_PROXY, body=body) or {}
    if response.get("status") == "err":
        raise AuthError(f"Perps refused to revoke the key: {response.get('error')}")
