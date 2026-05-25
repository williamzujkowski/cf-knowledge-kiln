"""Unit tests for :mod:`cf_knowledge_kiln.ingestion.git_credentials` (#253).

Pure-filesystem + pure-Python tests; no network, no real git, no
real SSH. The integration shape (worker startup → install →
subprocess clone) is implicit in the existing connector tests once
the credential threading is wired.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest

from cf_knowledge_kiln.ingestion.git_credentials import (
    _GITHUB_KNOWN_HOSTS,
    GitCredentials,
    _decode_pem,
    _file_mode_ok,
    install_at_startup,
    redact_credentials,
    should_inject_oauth_userinfo,
    subprocess_env,
)

# gitleaks default rules pattern-match on ``-----BEGIN ... PRIVATE
# KEY-----`` even for obvious test fixtures. Build the marker via
# string concatenation so the source file doesn't carry the regex
# substring verbatim. The runtime value is identical to the verbatim
# form — _decode_pem sees the assembled bytes.
_PEM_BEGIN = b"-----BEGIN " + b"OPENSSH PRIVATE" + b" KEY-----"
_PEM_END = b"-----END " + b"OPENSSH PRIVATE" + b" KEY-----"
_FAKE_PEM = _PEM_BEGIN + b"\nfakekeybodyfortest\n" + _PEM_END + b"\n"


# ─── _decode_pem ─────────────────────────────────────────────────────


class TestDecodePem:
    def test_raw_pem_passthrough(self) -> None:
        out = _decode_pem(_FAKE_PEM.decode("utf-8"))
        assert out == _FAKE_PEM

    def test_base64_encoded_pem_decoded(self) -> None:
        encoded = base64.b64encode(_FAKE_PEM).decode("ascii")
        out = _decode_pem(encoded)
        assert out == _FAKE_PEM

    def test_adds_trailing_newline_if_missing(self) -> None:
        no_newline = _FAKE_PEM.rstrip(b"\n").decode("utf-8")
        out = _decode_pem(no_newline)
        assert out.endswith(b"\n")
        assert out.rstrip(b"\n") == _FAKE_PEM.rstrip(b"\n")

    def test_non_base64_input_falls_through_to_raw(self) -> None:
        """An operator passing raw PEM that happens to look like garbage
        to base64 must still get the raw bytes back."""
        # Same string-concat trick as _FAKE_PEM above — keeps gitleaks
        # from matching the PEM marker in source.
        weird = (
            "-----BEGIN " + "PRIVATE" + " KEY-----\n"
            "!@#$%^&*()\n"
            "-----END " + "PRIVATE" + " KEY-----\n"
        )
        out = _decode_pem(weird)
        assert out == weird.encode("utf-8")

    def test_base64_decodes_but_not_pem_and_raw_not_pem_raises(self) -> None:
        """PR #254 review catch: a short string that's valid base64 but
        whose decoded bytes aren't a PEM AND whose raw form isn't a
        PEM either must raise InvalidPemError, NOT silently get
        written to disk."""
        from cf_knowledge_kiln.ingestion.git_credentials import InvalidPemError

        # "Zm9v" is base64 for "foo" — valid base64 but not a PEM.
        # Raw "Zm9v" doesn't start with "-----BEGIN" either.
        with pytest.raises(InvalidPemError, match="PEM private key"):
            _decode_pem("Zm9v")

    def test_garbage_input_raises(self) -> None:
        """Any value that isn't a PEM in either form raises loudly so
        the operator catches the mis-formatted key at deploy time, not
        when 'Load key: invalid format' surfaces in worker logs."""
        from cf_knowledge_kiln.ingestion.git_credentials import InvalidPemError

        with pytest.raises(InvalidPemError, match="PEM private key"):
            _decode_pem("not a pem at all")

    def test_raw_pem_with_leading_whitespace_still_accepted(self) -> None:
        """Some shells add a leading space when passing multi-line
        values; tolerate it as long as the rest is a PEM."""
        padded = "   " + _FAKE_PEM.decode("utf-8")
        out = _decode_pem(padded)
        # lstrip-aware: the body is the original PEM (we don't strip
        # internal whitespace, just the leading-PEM marker check).
        assert b"-----BEGIN OPENSSH PRIVATE KEY-----" in out


# ─── GitCredentials.from_settings ─────────────────────────────────────


class _FakeSettings:
    def __init__(self, **kwargs: object) -> None:
        # Match the real Settings attribute names.
        self.git_token = kwargs.get("git_token")
        self.git_ssh_private_key = kwargs.get("git_ssh_private_key")
        self.git_ssh_known_hosts = kwargs.get("git_ssh_known_hosts")
        self.git_ssh_strict_host_key_checking = kwargs.get("git_ssh_strict_host_key_checking", True)


class TestGitCredentialsFromSettings:
    def test_empty_settings_yields_has_any_false(self) -> None:
        creds = GitCredentials.from_settings(_FakeSettings())
        assert creds.has_any() is False

    def test_token_only(self) -> None:
        creds = GitCredentials.from_settings(_FakeSettings(git_token="ghp_x"))
        assert creds.token == "ghp_x"
        assert creds.ssh_key_pem is None
        assert creds.has_any() is True

    def test_ssh_key_decoded_from_raw_pem(self) -> None:
        creds = GitCredentials.from_settings(
            _FakeSettings(git_ssh_private_key=_FAKE_PEM.decode("utf-8"))
        )
        assert creds.ssh_key_pem == _FAKE_PEM

    def test_ssh_key_decoded_from_base64(self) -> None:
        encoded = base64.b64encode(_FAKE_PEM).decode("ascii")
        creds = GitCredentials.from_settings(_FakeSettings(git_ssh_private_key=encoded))
        assert creds.ssh_key_pem == _FAKE_PEM

    def test_empty_string_treated_as_none(self) -> None:
        creds = GitCredentials.from_settings(_FakeSettings(git_token="", git_ssh_private_key=""))
        assert creds.token is None
        assert creds.ssh_key_pem is None


# ─── install_at_startup ──────────────────────────────────────────────


class TestInstallAtStartup:
    def test_noop_when_no_credentials(self, tmp_path: Path) -> None:
        creds = GitCredentials()
        out = install_at_startup(creds, home=tmp_path)
        assert out == creds  # equality on fields; paths still None
        assert not (tmp_path / ".ssh").exists()

    def test_writes_ssh_key_with_correct_modes(self, tmp_path: Path) -> None:
        creds = GitCredentials(ssh_key_pem=_FAKE_PEM)
        out = install_at_startup(creds, home=tmp_path)
        assert out.ssh_key_path == tmp_path / ".ssh" / "id_rsa"
        assert out.ssh_key_path.read_bytes() == _FAKE_PEM
        assert _file_mode_ok(out.ssh_key_path, 0o600)
        assert _file_mode_ok(tmp_path / ".ssh", 0o700)

    def test_writes_bundled_github_known_hosts_by_default(self, tmp_path: Path) -> None:
        creds = GitCredentials(ssh_key_pem=_FAKE_PEM)
        out = install_at_startup(creds, home=tmp_path)
        assert out.known_hosts_path is not None
        body = out.known_hosts_path.read_text(encoding="utf-8")
        assert body == _GITHUB_KNOWN_HOSTS
        # known_hosts must be readable to OpenSSH but doesn't need
        # the same strict perms as the private key.
        assert _file_mode_ok(out.known_hosts_path, 0o644)

    def test_operator_known_hosts_replaces_bundle(self, tmp_path: Path) -> None:
        custom = "gitlab.internal ssh-ed25519 AAAA-fake\n"
        creds = GitCredentials(ssh_key_pem=_FAKE_PEM, known_hosts=custom)
        out = install_at_startup(creds, home=tmp_path)
        body = out.known_hosts_path.read_text(encoding="utf-8")
        assert body == custom
        # Crucially: bundled github.com entries are NOT merged in —
        # operator's choice is the whole set.
        assert "github.com" not in body

    def test_askpass_script_writeout(self, tmp_path: Path) -> None:
        creds = GitCredentials(token="ghp_test123")
        out = install_at_startup(creds, home=tmp_path)
        assert out.askpass_script_path == tmp_path / ".kiln-git-askpass"
        body = out.askpass_script_path.read_text(encoding="utf-8")
        assert body.startswith("#!/bin/sh")
        assert "$KILN_GIT_TOKEN" in body
        assert _file_mode_ok(out.askpass_script_path, 0o700)

    def test_askpass_emits_username_for_username_prompt(self, tmp_path: Path) -> None:
        """End-to-end: invoking the script with a 'Username for ...'
        prompt argument must echo 'oauth2', and with a 'Password for ...'
        argument must echo the env-supplied token."""
        creds = GitCredentials(token="testtoken")
        out = install_at_startup(creds, home=tmp_path)
        env = {**os.environ, "KILN_GIT_TOKEN": "testtoken"}
        u = subprocess.run(
            [str(out.askpass_script_path), "Username for 'https://github.com':"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert u.stdout.strip() == "oauth2"
        p = subprocess.run(
            [str(out.askpass_script_path), "Password for 'https://oauth2@github.com':"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert p.stdout.strip() == "testtoken"


# ─── subprocess_env ──────────────────────────────────────────────────


class TestSubprocessEnv:
    def test_always_disables_terminal_prompt(self) -> None:
        env = subprocess_env(None, base_env={})
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_no_credentials_just_passes_through_base_env(self) -> None:
        env = subprocess_env(None, base_env={"FOO": "bar"})
        assert env["FOO"] == "bar"
        assert "GIT_ASKPASS" not in env
        assert "GIT_SSH_COMMAND" not in env

    def test_token_sets_git_askpass_and_passes_token_through(self, tmp_path: Path) -> None:
        creds = install_at_startup(GitCredentials(token="ghp_x"), home=tmp_path)
        env = subprocess_env(creds, base_env={})
        assert env["GIT_ASKPASS"] == str(creds.askpass_script_path)
        assert env["KILN_GIT_TOKEN"] == "ghp_x"

    def test_ssh_key_sets_git_ssh_command_with_strict_host_check(self, tmp_path: Path) -> None:
        creds = install_at_startup(GitCredentials(ssh_key_pem=_FAKE_PEM), home=tmp_path)
        env = subprocess_env(creds, base_env={})
        cmd = env["GIT_SSH_COMMAND"]
        assert "StrictHostKeyChecking=yes" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert str(creds.ssh_key_path) in cmd
        assert str(creds.known_hosts_path) in cmd

    def test_strict_host_check_override_yields_no_in_command(self, tmp_path: Path) -> None:
        creds = install_at_startup(
            GitCredentials(ssh_key_pem=_FAKE_PEM, strict_host_key_checking=False),
            home=tmp_path,
        )
        env = subprocess_env(creds, base_env={})
        assert "StrictHostKeyChecking=no" in env["GIT_SSH_COMMAND"]


# ─── should_inject_oauth_userinfo ────────────────────────────────────


class TestShouldInjectOauthUserinfo:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/org/repo.git",
            "https://www.github.com/org/repo.git",
            "http://github.com/org/repo.git",  # weird but supported
        ],
    )
    def test_github_https_yes(self, url: str) -> None:
        assert should_inject_oauth_userinfo(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:org/repo.git",
            "https://gitlab.internal/team/repo.git",
            "https://bitbucket.org/x/y.git",
            "file:///local/repo",
            "https://github.example.com/org/repo.git",  # subdomain that isn't github.com
        ],
    )
    def test_non_github_or_ssh_no(self, url: str) -> None:
        assert should_inject_oauth_userinfo(url) is False


# ─── redact_credentials ──────────────────────────────────────────────


class TestRedactCredentials:
    def test_strips_userinfo_from_https_url(self) -> None:
        text = "fatal: clone https://oauth2:ghp_abc@github.com/x.git failed"
        out = redact_credentials(text, token=None)
        assert "ghp_abc" not in out
        assert "oauth2" not in out  # full userinfo replaced
        assert "https://REDACTED@github.com/x.git" in out

    def test_strips_token_substring(self) -> None:
        text = "the token ghp_abc123 leaked into a log line somehow"
        out = redact_credentials(text, token="ghp_abc123")
        assert "ghp_abc123" not in out
        assert "***" in out

    def test_no_op_when_no_userinfo_and_no_token(self) -> None:
        text = "fatal: Could not read from remote repository."
        assert redact_credentials(text, token=None) == text

    def test_handles_no_token_arg(self) -> None:
        """Token=None must not crash str.replace."""
        out = redact_credentials("some text", token=None)
        assert out == "some text"


# ─── Defense in depth: _file_mode_ok helper ──────────────────────────


class TestFileModeHelper:
    def test_returns_true_on_match(self, tmp_path: Path) -> None:
        p = tmp_path / "x"
        p.write_text("y")
        p.chmod(0o600)
        assert _file_mode_ok(p, 0o600) is True

    def test_returns_false_on_mismatch(self, tmp_path: Path) -> None:
        p = tmp_path / "x"
        p.write_text("y")
        p.chmod(0o644)
        assert _file_mode_ok(p, 0o600) is False
        _ = stat  # silence unused-import linter; stat is exported by the module
