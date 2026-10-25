import base64
import json
import os
import subprocess

import pytest

from remediation import ghapp


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    subprocess.run(["openssl", "genrsa", "-out", str(d / "k.pem"), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(d / "k.pem"), "-pubout", "-out", str(d / "k.pub")],
                   check=True, capture_output=True)
    return d / "k.pem", d / "k.pub"


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_is_a_valid_rs256_signature(keypair, tmp_path):
    key, pub = keypair
    token = ghapp.app_jwt("12345", key.read_text(), now=1_000_000)
    header, payload, sig = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_unb64(payload)) == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}
    (tmp_path / "sig").write_bytes(_unb64(sig))
    verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp_path / "sig")],
                              input=f"{header}.{payload}".encode(), capture_output=True)
    assert verified.returncode == 0, verified.stdout


def test_configured_reads_key_file_or_contents(monkeypatch, keypair):
    for var in ("REMEDIATION_APP_ID", "REMEDIATION_APP_KEY", "REMEDIATION_APP_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    assert ghapp.configured() is None
    monkeypatch.setenv("REMEDIATION_APP_ID", "7")
    monkeypatch.setenv("REMEDIATION_APP_KEY_FILE", str(keypair[0]))
    assert ghapp.configured()[0] == "7" and "PRIVATE KEY" in ghapp.configured()[1]
    monkeypatch.setenv("REMEDIATION_APP_KEY_FILE", "/nope.pem")
    with pytest.raises(ghapp.AppError, match="doesn't exist"):
        ghapp.configured()


def test_acting_as_app_sets_and_restores_gh_token(monkeypatch):
    monkeypatch.setenv("REMEDIATION_APP_ID", "7")
    monkeypatch.setenv("REMEDIATION_APP_KEY", "pem")
    monkeypatch.setenv("GH_TOKEN", "mine")
    monkeypatch.setattr(ghapp, "installation_token", lambda app_id, key, repo: f"bot-token-for-{repo}")
    with ghapp.acting_as_app("o/r") as token:
        assert os.environ["GH_TOKEN"] == token == "bot-token-for-o/r"
    assert os.environ["GH_TOKEN"] == "mine"


def test_without_app_config_it_acts_as_the_signed_in_user(monkeypatch):
    for var in ("REMEDIATION_APP_ID", "REMEDIATION_APP_KEY", "REMEDIATION_APP_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    with ghapp.acting_as_app("o/r") as token:
        assert token is None
    with pytest.raises(ghapp.AppError):
        with ghapp.acting_as_app("o/r", required=True):
            pass


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "https://github.com/o/r"])
def test_repo_slug(tmp_path, url):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    assert ghapp.repo_slug(tmp_path) == "o/r"
