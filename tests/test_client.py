import base64
import json

import pytest

from app.certs import fingerprint, generate_self_signed
from app.incus import (
    IncusClient,
    IncusConnectionError,
    IncusError,
    decode_trust_token,
    enroll_with_token,
)

from .fake_incus import serve_tls


def test_lists_instances_over_unix_socket(fake, socket_path):
    fake.add_instance("web-1")
    client = IncusClient(socket_path=socket_path)
    [instance] = client.instances()
    assert instance["name"] == "web-1"
    assert instance["state"]["network"]["eth0"]["addresses"][0]["address"].startswith("10.0.3.")


def test_action_waits_for_operation(fake, socket_path):
    fake.add_instance("web-1")
    client = IncusClient(socket_path=socket_path)
    assert client.wait(client.instance_action("web-1", "stop")) is True
    assert client.instance("web-1")["status"] == "Stopped"


def test_failed_operation_raises(fake, socket_path):
    fake.add_instance("web-1")
    client = IncusClient(socket_path=socket_path)
    with pytest.raises(IncusError, match="already running"):
        client.wait(client.instance_action("web-1", "start"))


def test_running_operation_reports_not_finished(fake, socket_path):
    fake.add_instance("web-1")
    fake.pending_ops = True
    client = IncusClient(socket_path=socket_path)
    assert client.wait(client.instance_action("web-1", "stop"), timeout=1) is False


def test_api_error_carries_message(fake, socket_path):
    client = IncusClient(socket_path=socket_path)
    with pytest.raises(IncusError) as info:
        client.instance("missing")
    assert info.value.status == 404
    assert "not found" in info.value.message


def test_names_are_percent_encoded(fake, socket_path):
    client = IncusClient(socket_path=socket_path)
    with pytest.raises(IncusError):
        client.instance("a/b?c")
    assert fake.calls[-1] == ("GET", "/1.0/instances/a%2Fb%3Fc")


def test_missing_socket_is_a_connection_error(short_dir):
    client = IncusClient(socket_path=f"{short_dir}/nope.sock")
    with pytest.raises(IncusConnectionError):
        client.server_info()


@pytest.fixture
def tls(fake, tmp_path):
    server_crt, server_key = tmp_path / "server.crt", tmp_path / "server.key"
    client_crt, client_key = tmp_path / "client.crt", tmp_path / "client.key"
    generate_self_signed("fake-incus", server_crt, server_key, server=True)
    generate_self_signed("incus-console", client_crt, client_key)
    server = serve_tls(fake, str(server_crt), str(server_key), client_ca=str(client_crt))
    yield {
        "url": f"https://127.0.0.1:{server.server_address[1]}",
        "address": f"127.0.0.1:{server.server_address[1]}",
        "fingerprint": fingerprint(server_crt),
        "cert": str(client_crt),
        "key": str(client_key),
    }
    server.shutdown()
    server.server_close()


def test_https_with_pinned_fingerprint(fake, tls):
    client = IncusClient(
        url=tls["url"], cert=tls["cert"], key=tls["key"], fingerprint=tls["fingerprint"]
    )
    assert client.server_info()["auth"] == "trusted"


def test_https_rejects_wrong_fingerprint(tls):
    client = IncusClient(url=tls["url"], cert=tls["cert"], key=tls["key"], fingerprint="ab" * 32)
    with pytest.raises(IncusConnectionError, match="fingerprint"):
        client.server_info()


def test_https_without_pin_rejects_self_signed(tls):
    client = IncusClient(url=tls["url"], cert=tls["cert"], key=tls["key"])
    with pytest.raises(IncusConnectionError):
        client.server_info()


def _token(tls, **extra):
    data = {
        "client_name": "incus-console",
        "fingerprint": tls["fingerprint"],
        "addresses": ["127.0.0.1:1", tls["address"]],
        "secret": "s3cret",
        **extra,
    }
    return base64.b64encode(json.dumps(data).encode()).decode()


def test_enroll_with_token_presents_client_certificate(fake, tls):
    token = _token(tls)
    fake.tokens.add(token)
    result = enroll_with_token(token, tls["cert"], tls["key"], timeout=5)
    assert result["url"] == tls["url"]  # skipped the unreachable first address
    assert fake.trusted == [fingerprint(tls["cert"])]


def test_enroll_accepts_a_server_that_already_trusts_us(fake, tls):
    first, second = _token(tls), _token(tls, secret="again")
    fake.tokens.update({first, second})
    enroll_with_token(first, tls["cert"], tls["key"], timeout=5)
    result = enroll_with_token(second, tls["cert"], tls["key"], timeout=5)
    assert result["url"] == tls["url"] and result["fingerprint"] == tls["fingerprint"]
    assert fake.trusted == [fingerprint(tls["cert"])]


def test_enroll_refuses_server_that_does_not_match_token(fake, tls):
    token = _token(tls, fingerprint="cd" * 32)
    fake.tokens.add(token)
    with pytest.raises(IncusConnectionError):
        enroll_with_token(token, tls["cert"], tls["key"], timeout=5)
    assert fake.trusted == []


def test_bad_token_is_reported():
    with pytest.raises(IncusError, match="trust token"):
        decode_trust_token("not a token")
