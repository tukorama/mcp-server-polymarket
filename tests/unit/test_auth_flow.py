import argparse
import json

import pytest

pytest.importorskip("eth_account")

from mcp_server_polymarket import auth_flow  # noqa: E402
from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.client.base import ApiClient  # noqa: E402
from mcp_server_polymarket.client.session import CredentialStore  # noqa: E402
from mcp_server_polymarket.errors import Refused  # noqa: E402
from mcp_server_polymarket.identity import Identity  # noqa: E402
from mcp_server_polymarket.signing import clob, eip712, perps, relayer  # noqa: E402
from mcp_server_polymarket.signing.wallets import derive_deposit_wallet  # noqa: E402

pytestmark = pytest.mark.anyio
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
SESSION_KEY = "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"


@pytest.fixture
def signer(settings):
    settings.private_key = KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    settings.relayer_api_key, settings.relayer_api_key_address = "rk", OWNER
    return settings


def parts(settings, api):
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    identity = Identity(settings, store)
    return identity, ApiClient(settings, identity, transport=api.transport), store


def args(**kwargs):
    defaults = {"read_only": False, "days": 7, "yes": True, "perps": False}
    return argparse.Namespace(**{**defaults, **kwargs})


async def test_derive_existing_clob_key(signer, api):
    api.add(ep.CLOB_DERIVE_API_KEY, {"apiKey": "k", "secret": "s2VjcmV0", "passphrase": "p"})
    identity, client, store = parts(signer, api)
    result = await auth_flow.clob_credentials(identity, client, store)
    assert result["status"] == "derived" and result["signer"] == OWNER
    stored = store.load_clob(OWNER)
    assert stored and stored.api_key == "k"
    request = api.calls(ep.CLOB_DERIVE_API_KEY)[0]
    typed = clob.clob_auth_typed_data(OWNER, int(request.headers["POLY_TIMESTAMP"]))
    assert eip712.recover(typed, request.headers["POLY_SIGNATURE"]) == OWNER
    assert (store.session_file.stat().st_mode & 0o777) == 0o600
    assert "s2VjcmV0" not in json.dumps(result)


async def test_create_when_nothing_to_derive(signer, api):
    api.add(ep.CLOB_DERIVE_API_KEY, (400, {"error": "Could not derive api key!"}))
    api.add(ep.CLOB_CREATE_API_KEY, {"apiKey": "new", "secret": "c2VjcmV0", "passphrase": "p"})
    identity, client, store = parts(signer, api)
    assert (await auth_flow.clob_credentials(identity, client, store))["status"] == "created"
    assert store.load_clob(OWNER).api_key == "new"


async def test_perps_session_is_owner_signed_and_stored(signer, api):
    api.add(ep.PERPS_REGISTERED, {"registered": True})
    api.add(ep.PERPS_CREATE_PROXY, {"status": "ok", "secret": "perps-secret"})
    identity, client, store = parts(signer, api)
    result = await auth_flow.create_perps_session(identity, client, store, args(perps=True))
    body = api.body(ep.PERPS_CREATE_PROXY)
    proxy = body["op"]["args"]["proxy"]
    assert body["op"]["args"]["owner"] == OWNER and result["proxy"] == proxy
    typed = perps.create_proxy_typed_data(
        proxy, body["op"]["args"]["expiry"], body["salt"], body["ts"]
    )
    assert eip712.recover(typed, body["sig"]) == OWNER
    saved = store.load_perps()
    assert saved.secret == "perps-secret" and saved.can_sign
    assert eip712.address_of(saved.proxy_key) == proxy


async def test_read_only_perps_session_keeps_no_signing_key(signer, api):
    api.add(ep.PERPS_REGISTERED, {"registered": True})
    api.add(ep.PERPS_CREATE_PROXY, {"status": "ok", "secret": "s"})
    identity, client, store = parts(signer, api)
    await auth_flow.create_perps_session(identity, client, store, args(perps=True, read_only=True))
    assert api.body(ep.PERPS_CREATE_PROXY)["label"].startswith("read-only:")
    assert store.load_perps().proxy_key is None


async def test_declining_creates_nothing(signer, api, monkeypatch):
    api.add(ep.PERPS_REGISTERED, {"registered": False})
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    identity, client, store = parts(signer, api)
    with pytest.raises(Refused):
        await auth_flow.create_perps_session(identity, client, store, args(perps=True, yes=False))
    assert api.calls(ep.PERPS_CREATE_PROXY) == []


async def test_authorise_session_key(signer, api):
    api.add(ep.RELAYER_TX_PARAMS, {"address": OWNER, "nonce": "7"})
    api.add(ep.RELAYER_SESSION_AUTHORIZE, {"status": "accepted", "transactionID": "t"})
    identity, client, _ = parts(signer, api)
    result = await auth_flow.change_session_key(identity, client, SESSION_KEY, True, True)
    assert result["session_key"] == SESSION_KEY
    request = api.calls(ep.RELAYER_SESSION_AUTHORIZE)[0]
    assert request.headers["Idempotency-Key"]
    body = json.loads(request.content)
    assert body["scopes"] == ["ALL"] and body["nonce"] == "7"
    wallet = signer.wallet_address
    call = relayer.authorize_session_signer(wallet, SESSION_KEY, int(body["validUntil"]))
    typed = relayer.batch_typed_data(wallet, [call], 7, int(body["deadline"]))
    assert eip712.recover(typed, body["signature"]) == OWNER


async def test_revoke_session_key(signer, api):
    api.add(ep.RELAYER_TX_PARAMS, {"nonce": "8"})
    api.add(ep.RELAYER_SESSION_REVOKE, {"status": "accepted"})
    identity, client, _ = parts(signer, api)
    await auth_flow.change_session_key(identity, client, SESSION_KEY, False, True)
    assert "scopes" not in api.body(ep.RELAYER_SESSION_REVOKE)


async def test_builder_key_is_written_to_a_private_file(signer, api, tmp_path):
    from mcp_server_polymarket.client.session import ClobCredentials

    identity, client, store = parts(signer, api)
    store.save_clob(ClobCredentials(OWNER, "k", "c2VjcmV0", "p"))
    api.add(ep.CLOB_CREATE_BUILDER_KEY, {"key": "bk", "secret": "bs", "passphrase": "bp"})
    result = await auth_flow.create_builder_key(identity, client)
    assert "bs" not in json.dumps(result)
    with open(result["saved_to"]) as handle:
        assert json.load(handle)["secret"] == "bs"


async def test_logout_revokes_perps_with_the_owner_key(signer, api, tmp_path):
    from mcp_server_polymarket.client.session import PerpsSession

    store = CredentialStore(signer.resolved_session_file, signer.resolved_perps_session_file)
    store.save_perps(PerpsSession(owner=OWNER, proxy=SESSION_KEY, secret="s"))
    api.add(ep.PERPS_DELETE_PROXY, {"status": "ok"})
    identity, client, _ = parts(signer, api)
    await auth_flow.revoke_perps_session(identity, client, store.load_perps())
    body = api.body(ep.PERPS_DELETE_PROXY)
    assert body["op"] == {"type": "deleteProxy", "args": {"proxy": SESSION_KEY}}
    typed = perps.op_typed_data(
        perps.delete_proxy(SESSION_KEY), salt=body["salt"], ts_ms=body["ts"]
    )
    assert eip712.recover(typed, body["sig"]) == OWNER
