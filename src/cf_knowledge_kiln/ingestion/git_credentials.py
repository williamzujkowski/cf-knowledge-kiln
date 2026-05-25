"""Git credential management for the worker's ``git clone`` calls (#253).

The worker shells out to ``git clone`` for every git-typed source. Public
repos work out of the box; private repos need credentials the kiln didn't
ship a way to provide. homelab-iac PRs #704 → #710 iterated through
shell-script workarounds to wire SSH keys into the CF worker container —
each was a workaround for an upstream blind spot this module closes.

Two opt-in mechanisms, both off by default (settings fields default to
``None``):

1. ``KILN_GIT_TOKEN`` — GitHub PAT injected via ``GIT_ASKPASS`` so the
   token never lands on the git command line (``/proc/<pid>/cmdline``
   stays clean). Scoped to ``github.com`` URLs at clone time so a token
   intended for github.com is never silently forwarded to another host.
2. ``KILN_GIT_SSH_PRIVATE_KEY`` — written to ``~/.ssh/id_rsa`` (mode
   0600) at worker startup, with bundled GitHub host keys in
   ``~/.ssh/known_hosts`` so ``StrictHostKeyChecking=yes`` works
   out of the box. Operator-supplied ``KILN_GIT_SSH_KNOWN_HOSTS``
   replaces the bundled entries (no merging — explicit contract).

The PEM field auto-detects base64 vs raw — homelab-iac discovered the
hard way that multi-line values through ``cf set-env`` work but are
shell-fragile, so base64 is the friendlier wire format.

Security guarantees:

* SSH key: file mode 0600, ``~/.ssh`` dir mode 0700, never logged.
* Token: passed via env to the askpass helper, never on argv.
* Stderr from git is run through :func:`redact_credentials` before
  surfacing in exceptions / logs — defense in depth against any
  pathological URL or message that included the credential.
* ``StrictHostKeyChecking`` defaults to ``yes`` and the operator
  override is logged at WARNING so MITM-able SSH is loud, not silent.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Bundled GitHub host keys. Sourced from https://api.github.com/meta
# (the ``ssh_keys`` field) on 2026-05-25. Refreshing requires an
# upstream commit; a CI test (separate follow-up) can fetch the live
# values and assert they match these to catch GitHub rotations.
#
# Format is the standard OpenSSH ``known_hosts`` line:
#     <hostname[,alt-hostname]> <key-type> <base64-key>
_GITHUB_KNOWN_HOSTS = """\
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
"""


_GITHUB_HOSTS_FOR_TOKEN_REWRITE = {"github.com", "www.github.com"}


@dataclass(frozen=True)
class GitCredentials:
    """Resolved credential bundle for the worker's git clones.

    ``ssh_key_pem`` is bytes (the on-disk key) — the env-supplied
    string is decoded by :meth:`from_settings` either as base64 or
    raw PEM.
    """

    token: str | None = None
    ssh_key_pem: bytes | None = None
    known_hosts: str | None = None
    strict_host_key_checking: bool = True
    # Internal: paths written by ``install_at_startup`` so
    # ``subprocess_env`` can reference them. Populated by install.
    askpass_script_path: Path | None = field(default=None, compare=False)
    ssh_key_path: Path | None = field(default=None, compare=False)
    known_hosts_path: Path | None = field(default=None, compare=False)

    @classmethod
    def from_settings(cls, settings: object) -> GitCredentials:
        """Build a credential bundle from ``Settings``-shaped object.

        Accepts ``object`` so the caller can pass either the real
        ``Settings`` or any test-shaped duck. The required attributes:
        ``git_token``, ``git_ssh_private_key``, ``git_ssh_known_hosts``,
        ``git_ssh_strict_host_key_checking``.
        """
        token = getattr(settings, "git_token", None)
        raw_key = getattr(settings, "git_ssh_private_key", None)
        known_hosts = getattr(settings, "git_ssh_known_hosts", None)
        strict = getattr(settings, "git_ssh_strict_host_key_checking", True)
        return cls(
            token=token or None,
            ssh_key_pem=_decode_pem(raw_key) if raw_key else None,
            known_hosts=known_hosts or None,
            strict_host_key_checking=bool(strict),
        )

    def has_any(self) -> bool:
        return self.token is not None or self.ssh_key_pem is not None


def _decode_pem(value: str) -> bytes:
    """Accept raw PEM or base64-encoded PEM; auto-detect on shape.

    The detection rule: try base64 decoding; if the result starts with
    ``-----BEGIN`` it's a base64-encoded PEM. Otherwise treat the input
    as raw PEM bytes. Catches the common case where the operator
    pipes ``base64 -w 0 key.pem | xargs cf set-env`` AND the case where
    they paste the multi-line PEM directly.

    Always returns bytes ending with a newline — OpenSSH refuses keys
    without the trailing newline and the error message is opaque.
    """
    candidate = value.strip()
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (ValueError, binascii.Error):
        # Not valid base64 at all — fall through to raw-PEM treatment.
        return _ensure_trailing_newline(value.encode("utf-8"))
    if decoded.startswith(b"-----BEGIN"):
        return _ensure_trailing_newline(decoded)
    # base64 decoded fine but the result isn't a PEM — operator passed
    # a non-base64 value that coincidentally validated. Trust the raw
    # input instead of the meaningless decoded bytes.
    return _ensure_trailing_newline(value.encode("utf-8"))


def _ensure_trailing_newline(b: bytes) -> bytes:
    return b if b.endswith(b"\n") else b + b"\n"


_ASKPASS_SCRIPT = """\
#!/bin/sh
# cf-knowledge-kiln GIT_ASKPASS helper (#253). Reads KILN_GIT_TOKEN
# from the child-process env (subprocess_env passes it through) and
# echoes the right value for git's HTTPS prompt sequence. The token
# never appears on the git command line.
case "$1" in
  Username*) echo "oauth2" ;;
  Password*) echo "$KILN_GIT_TOKEN" ;;
esac
"""


def install_at_startup(
    creds: GitCredentials,
    *,
    home: Path | None = None,
) -> GitCredentials:
    """Write SSH key / known_hosts / askpass helper to the home dir.

    Idempotent — safe to call on every worker restart. ``home``
    defaults to ``Path.home()``; tests pass a tmp dir.

    Returns a NEW :class:`GitCredentials` with the path fields
    populated so :func:`subprocess_env` can reference them.

    No-op when ``creds`` has nothing set — the kiln must still work
    for public-repo deployments that never set either env var.
    """
    if not creds.has_any():
        return creds

    home = home or Path.home()
    ssh_dir = home / ".ssh"
    ssh_key_path: Path | None = None
    known_hosts_path: Path | None = None
    askpass_path: Path | None = None

    if creds.ssh_key_pem is not None:
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        # Re-chmod even if the dir existed — operators may have created
        # ~/.ssh themselves with 0755 (common default on some images).
        ssh_dir.chmod(0o700)
        ssh_key_path = ssh_dir / "id_rsa"
        ssh_key_path.write_bytes(creds.ssh_key_pem)
        ssh_key_path.chmod(0o600)
        # Log size only — NEVER the key bytes or a fingerprint.
        logger.info(
            "git credentials: wrote SSH private key (%d bytes) to %s",
            len(creds.ssh_key_pem),
            ssh_key_path,
        )

        # known_hosts: operator override REPLACES the bundle (explicit
        # contract — no merging). When no override is given, ship the
        # bundled GitHub keys so a CF deploy hitting github.com from
        # a clean image just works.
        ssh_dir.mkdir(mode=0o700, exist_ok=True)  # belt-and-braces
        known_hosts_path = ssh_dir / "known_hosts"
        body = creds.known_hosts if creds.known_hosts is not None else _GITHUB_KNOWN_HOSTS
        known_hosts_path.write_text(body, encoding="utf-8")
        known_hosts_path.chmod(0o644)
        logger.info(
            "git credentials: wrote known_hosts to %s (%s)",
            known_hosts_path,
            "operator-supplied" if creds.known_hosts is not None else "bundled github.com keys",
        )

        if not creds.strict_host_key_checking:
            logger.warning(
                "git credentials: KILN_GIT_SSH_STRICT_HOST_KEY_CHECKING is false; "
                "SSH clones are MITM-able. Only use this to diagnose a host-key change."
            )

    if creds.token is not None:
        # askpass script lives in HOME (not ~/.ssh) so it's easy to
        # find for debug + outside the security envelope of the SSH
        # key dir. Mode 0700 (executable + private to the worker user).
        askpass_path = home / ".kiln-git-askpass"
        askpass_path.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        askpass_path.chmod(0o700)
        logger.info(
            "git credentials: wrote GIT_ASKPASS helper to %s (token-via-env)",
            askpass_path,
        )

    # Return a new instance with the path fields populated — the
    # dataclass is frozen so we can't mutate creds in place. This is
    # what callers thread through to the connector.
    return GitCredentials(
        token=creds.token,
        ssh_key_pem=creds.ssh_key_pem,
        known_hosts=creds.known_hosts,
        strict_host_key_checking=creds.strict_host_key_checking,
        askpass_script_path=askpass_path,
        ssh_key_path=ssh_key_path,
        known_hosts_path=known_hosts_path,
    )


def subprocess_env(
    creds: GitCredentials | None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict for a ``subprocess.run`` git invocation.

    Always sets ``GIT_TERMINAL_PROMPT=0`` so a private-repo clone
    without credentials fails fast instead of hanging on a TTY
    prompt. Adds ``GIT_ASKPASS`` when a token is configured, and
    ``GIT_SSH_COMMAND`` when an SSH key is configured.

    ``base_env`` defaults to ``os.environ``. Explicit so tests can
    inject a clean env without leaking the test runner's env vars.
    """
    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)
    # Always disable terminal prompts — public-repo clones still work
    # (no prompt is ever needed), and a bad private-repo config fails
    # fast instead of hanging the worker forever waiting for input.
    env["GIT_TERMINAL_PROMPT"] = "0"

    if creds is None or not creds.has_any():
        return env

    if creds.askpass_script_path is not None and creds.token is not None:
        env["GIT_ASKPASS"] = str(creds.askpass_script_path)
        # Pass the token to the child explicitly — the askpass script
        # reads it from its env.
        env["KILN_GIT_TOKEN"] = creds.token

    if creds.ssh_key_path is not None:
        # IdentitiesOnly=yes prevents ssh-agent from offering other
        # keys; UserKnownHostsFile pins the bundled or operator-
        # supplied known_hosts; StrictHostKeyChecking gates MITM.
        strict = "yes" if creds.strict_host_key_checking else "no"
        ssh_cmd_parts = [
            "ssh",
            "-o",
            f"StrictHostKeyChecking={strict}",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(creds.ssh_key_path),
        ]
        if creds.known_hosts_path is not None:
            ssh_cmd_parts.extend(["-o", f"UserKnownHostsFile={creds.known_hosts_path}"])
        env["GIT_SSH_COMMAND"] = " ".join(ssh_cmd_parts)

    return env


def should_inject_oauth_userinfo(url: str) -> bool:
    """Return True if the URL is a github.com HTTPS URL eligible for token use.

    We're conservative: only HTTPS URLs to ``github.com`` or
    ``www.github.com`` get the askpass treatment. SSH URLs
    (``git@github.com:...``) and non-GitHub HTTPS URLs flow through
    unchanged — the token is never forwarded to a host it wasn't
    intended for.
    """
    if not url.startswith(("http://", "https://")):
        return False
    # Strip scheme and userinfo to get the host.
    after_scheme = url.split("://", 1)[1]
    host_and_path = after_scheme.split("@", 1)[-1]
    host = host_and_path.split("/", 1)[0].split(":", 1)[0].lower()
    return host in _GITHUB_HOSTS_FOR_TOKEN_REWRITE


_URL_USERINFO_RE = re.compile(r"(https?://)[^:/@\s]+:[^@\s]+@")


def redact_credentials(text: str, token: str | None = None) -> str:
    """Strip credentials from text before logging / raising.

    Two passes:

    1. URL userinfo: ``https://user:pwd@host/...`` → ``https://REDACTED@host/...``.
       Catches accidentally-pre-rewritten URLs that show up in git
       stderr from non-askpass code paths.
    2. Token substring: if ``token`` is set and appears anywhere in
       ``text``, replace it with ``***``. Defense in depth against
       any pathological message that included the credential.
    """
    out = _URL_USERINFO_RE.sub(r"\1REDACTED@", text)
    if token:
        out = out.replace(token, "***")
    return out


def _file_mode_ok(path: Path, expected: int) -> bool:
    """Helper for tests — returns whether the file's permission bits match."""
    return stat.S_IMODE(path.stat().st_mode) == expected


__all__ = [
    "GitCredentials",
    "install_at_startup",
    "redact_credentials",
    "should_inject_oauth_userinfo",
    "subprocess_env",
]
