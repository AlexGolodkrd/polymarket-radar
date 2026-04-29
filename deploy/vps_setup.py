"""VPS bootstrap for plan-kapkan arbitrage radar.

Executes a phased deploy via paramiko:

  Phase A — hardening (uses root + password ONCE, then locks down):
    1. apt update; install base packages
    2. push admin SSH pubkey -> /root/.ssh/authorized_keys
    3. create unprivileged 'arb' user (sudoer, same key)
    4. test key-based login is reachable
    5. disable PasswordAuthentication + permit-root-by-key-only
    6. ufw deny incoming default; allow ssh + http
    7. fail2ban enabled (sshd jail)

  Phase B — service infra (root via key):
    1. install docker-ce + compose plugin
    2. install nginx + apache2-utils
    3. write /etc/nginx/sites-available/arb-radar (reverse proxy + basic auth)
    4. write /etc/nginx/.htpasswd (user/random pass — printed once)
    5. add fail2ban jail nginx-http-auth

  Phase C — service deploy (user 'arb' via key):
    1. generate per-VPS deploy key, register it in the repo via GitHub API (PAT)
    2. git clone via deploy key into /home/arb/plan-kapkan
    3. write minimal Credentials.env on host (DRY_RUN=1, WALLET_BACKEND=local)
    4. docker compose up -d
    5. smoke-test http://127.0.0.1:5050/api/risk_status

Usage (from repo root):
    DEPLOY_PASS='...' python deploy/vps_setup.py --phase=all

The root password is read from env (DEPLOY_PASS) and is needed only for
phase A; afterwards the script switches to key-based auth.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import paramiko  # noqa: E402

VPS_HOST = "77.91.97.22"
ROOT_USER = "root"
APP_USER = "arb"
APP_DIR = f"/home/{APP_USER}/plan-kapkan"
REPO_OWNER = "AlexGolodkrd"
REPO_NAME = "plan-kapkan"
REPO_GIT_URL = f"git@github.com:{REPO_OWNER}/{REPO_NAME}.git"
LOCAL_PUBKEY_PATH = Path.home() / ".ssh" / "id_rsa.pub"
LOCAL_PRIVKEY_PATH = Path.home() / ".ssh" / "id_rsa"
NGINX_BASIC_USER = "admin"

# ───────────────────────────── helpers ──────────────────────────────


def _connect_password(password: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        VPS_HOST, username=ROOT_USER, password=password,
        timeout=30, allow_agent=False, look_for_keys=False,
    )
    return c


def _load_rsa_key() -> paramiko.RSAKey:
    """Load id_rsa explicitly as RSA — avoids paramiko 2.8.1's broken
    auto-detection that falls through to DSSKey on OpenSSH-format files."""
    return paramiko.RSAKey.from_private_key_file(str(LOCAL_PRIVKEY_PATH))


def _connect_key(user: str = ROOT_USER) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        VPS_HOST, username=user,
        pkey=_load_rsa_key(),
        timeout=30, allow_agent=False, look_for_keys=False,
        disabled_algorithms={"keys": ["ssh-dss"]},
    )
    return c


def _run(c: paramiko.SSHClient, cmd: str, *, sudo_pass: str | None = None,
         check: bool = True, quiet: bool = False) -> tuple[int, str, str]:
    """Run a shell command on the remote host. Returns (rc, stdout, stderr)."""
    stdin, stdout, stderr = c.exec_command(cmd, get_pty=False, timeout=600)
    if sudo_pass and cmd.startswith("sudo "):
        stdin.write(sudo_pass + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if not quiet:
        head = cmd if len(cmd) < 200 else cmd[:197] + "..."
        print(f"  $ {head}")
        if out.strip():
            for line in out.rstrip().splitlines()[:30]:
                print(f"    | {line}")
            if len(out.splitlines()) > 30:
                print(f"    | ... ({len(out.splitlines())-30} more lines)")
        if err.strip():
            for line in err.rstrip().splitlines()[:10]:
                print(f"    !! {line}")
    if check and rc != 0:
        raise RuntimeError(f"remote cmd failed (rc={rc}): {cmd}\nSTDERR: {err}")
    return rc, out, err


def _put(c: paramiko.SSHClient, local_path: Path, remote_path: str,
         mode: int | None = None) -> None:
    sftp = c.open_sftp()
    sftp.put(str(local_path), remote_path)
    if mode is not None:
        sftp.chmod(remote_path, mode)
    sftp.close()
    print(f"  + uploaded {local_path.name} -> {remote_path}")


def _put_text(c: paramiko.SSHClient, text: str, remote_path: str,
              mode: int | None = None) -> None:
    sftp = c.open_sftp()
    with sftp.open(remote_path, "w") as f:
        f.write(text)
    if mode is not None:
        sftp.chmod(remote_path, mode)
    sftp.close()
    print(f"  + wrote {len(text)}B -> {remote_path}")


def _gen_password(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ───────────────────────────── Phase A ──────────────────────────────


def phase_a_hardening(root_password: str) -> None:
    print("\n=== Phase A: hardening ===")
    pubkey = LOCAL_PUBKEY_PATH.read_text().strip()

    print("[A.1] connect via password & install base packages")
    c = _connect_password(root_password)
    try:
        _run(c, "apt-get update -y", quiet=False)
        _run(c, "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "openssh-server ufw fail2ban sudo curl ca-certificates gnupg")

        print("\n[A.2] push admin pubkey to /root/.ssh/authorized_keys")
        _run(c, "mkdir -p /root/.ssh && chmod 700 /root/.ssh")
        _put_text(c, pubkey + "\n", "/root/.ssh/authorized_keys", mode=0o600)

        print(f"\n[A.3] create unprivileged user '{APP_USER}' with sudo + same key")
        _run(c, f"id {APP_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {APP_USER}")
        _run(c, f"usermod -aG sudo {APP_USER}")
        _run(c, f"mkdir -p /home/{APP_USER}/.ssh && chmod 700 /home/{APP_USER}/.ssh")
        _put_text(c, pubkey + "\n", f"/home/{APP_USER}/.ssh/authorized_keys", mode=0o600)
        _run(c, f"chown -R {APP_USER}:{APP_USER} /home/{APP_USER}/.ssh")
        _run(c,
             f"echo '{APP_USER} ALL=(ALL) NOPASSWD:ALL' "
             f"> /etc/sudoers.d/90-{APP_USER} && chmod 440 /etc/sudoers.d/90-{APP_USER}")
    finally:
        c.close()

    print("\n[A.4] verify key-based login works (root + arb)")
    for u in (ROOT_USER, APP_USER):
        ck = _connect_key(u)
        try:
            rc, out, _ = _run(ck, "whoami", quiet=False)
            assert out.strip() == u, f"key login as {u} reported {out.strip()!r}"
        finally:
            ck.close()
    print("  ok: ssh key auth working for both root and arb")

    print("\n[A.5] lock sshd: disable PasswordAuthentication, "
          "PermitRootLogin prohibit-password")
    c = _connect_key(ROOT_USER)
    try:
        # Drop a dedicated config file in /etc/ssh/sshd_config.d/ so we
        # do not have to edit the main file (Ubuntu 24 sshd reads *.conf
        # from that dir already).
        sshd_conf = (
            "# plan-kapkan hardening — managed by deploy/vps_setup.py\n"
            "PasswordAuthentication no\n"
            "PermitRootLogin prohibit-password\n"
            "ChallengeResponseAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\n"
            "PermitEmptyPasswords no\n"
            "MaxAuthTries 4\n"
            "LoginGraceTime 30\n"
        )
        _put_text(c, sshd_conf, "/etc/ssh/sshd_config.d/10-plan-kapkan.conf",
                  mode=0o644)
        _run(c, "sshd -t")  # validate before restart
        _run(c, "systemctl restart ssh")

        print("\n[A.6] ufw — default deny in, allow ssh + http")
        _run(c, "ufw --force reset")
        _run(c, "ufw default deny incoming")
        _run(c, "ufw default allow outgoing")
        _run(c, "ufw allow 22/tcp")
        _run(c, "ufw allow 80/tcp")
        # 443 for future SSL — open now so renewal/reload doesn't surprise us
        _run(c, "ufw allow 443/tcp")
        _run(c, "ufw --force enable")
        _run(c, "ufw status verbose")

        print("\n[A.7] fail2ban — default sshd jail")
        jail = (
            "[DEFAULT]\n"
            "bantime  = 1h\n"
            "findtime = 10m\n"
            "maxretry = 5\n"
            "backend  = systemd\n"
            "\n"
            "[sshd]\n"
            "enabled = true\n"
            "port    = ssh\n"
        )
        _put_text(c, jail, "/etc/fail2ban/jail.local", mode=0o644)
        _run(c, "systemctl enable --now fail2ban")
        _run(c, "systemctl restart fail2ban")
        _run(c, "fail2ban-client status sshd || true")
    finally:
        c.close()
    print("\n=== Phase A done ===")


# ───────────────────────────── Phase B ──────────────────────────────


def phase_b_service_infra(basic_auth_password: str) -> None:
    print("\n=== Phase B: service infrastructure ===")
    c = _connect_key(ROOT_USER)
    try:
        print("[B.1] install Docker (docker-ce + compose v2 plugin)")
        # Use Docker's official apt repo for an up-to-date engine
        _run(c, "install -m 0755 -d /etc/apt/keyrings")
        _run(c,
             "curl -fsSL https://download.docker.com/linux/ubuntu/gpg "
             "| gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg")
        _run(c, "chmod a+r /etc/apt/keyrings/docker.gpg")
        _run(c,
             'echo "deb [arch=$(dpkg --print-architecture) '
             'signed-by=/etc/apt/keyrings/docker.gpg] '
             'https://download.docker.com/linux/ubuntu '
             '$(. /etc/os-release && echo $VERSION_CODENAME) stable" '
             "> /etc/apt/sources.list.d/docker.list")
        _run(c, "apt-get update -y")
        _run(c, "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "docker-ce docker-ce-cli containerd.io "
                "docker-buildx-plugin docker-compose-plugin")
        _run(c, f"usermod -aG docker {APP_USER}")
        _run(c, "systemctl enable --now docker")
        _run(c, "docker --version && docker compose version")

        print("\n[B.2] install nginx + apache2-utils (for htpasswd)")
        _run(c, "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "nginx apache2-utils")

        print(f"\n[B.3] create htpasswd ({NGINX_BASIC_USER}, generated pass)")
        _run(c, f"htpasswd -cb /etc/nginx/.htpasswd "
                f"{NGINX_BASIC_USER} '{basic_auth_password}'")
        _run(c, "chown root:www-data /etc/nginx/.htpasswd "
                "&& chmod 640 /etc/nginx/.htpasswd")

        print("\n[B.4] write nginx site for arb-radar (reverse proxy)")
        site = f"""# plan-kapkan arb-radar — nginx reverse proxy (basic auth)
upstream arb_radar_backend {{
    server 127.0.0.1:5050;
    keepalive 32;
}}

server {{
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name {VPS_HOST} _;

    # health probe — no auth, container-only path
    location = /api/risk_status {{
        proxy_pass http://arb_radar_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    # everything else is gated by basic auth
    location / {{
        auth_basic "plan-kapkan radar";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass http://arb_radar_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Server-Sent Events / streaming need these
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        # WebSocket upgrade
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}

    # client logs go to /var/log/nginx — fail2ban reads them
    access_log /var/log/nginx/arb-radar.access.log;
    error_log  /var/log/nginx/arb-radar.error.log warn;
}}
"""
        _put_text(c, site, "/etc/nginx/sites-available/arb-radar", mode=0o644)
        _run(c, "ln -sf /etc/nginx/sites-available/arb-radar "
                "/etc/nginx/sites-enabled/arb-radar")
        _run(c, "rm -f /etc/nginx/sites-enabled/default")
        _run(c, "nginx -t")
        _run(c, "systemctl enable --now nginx")
        _run(c, "systemctl reload nginx")

        print("\n[B.5] fail2ban jail for nginx-http-auth")
        nginx_jail = (
            "[nginx-http-auth]\n"
            "enabled = true\n"
            "port    = http,https\n"
            "logpath = /var/log/nginx/error.log /var/log/nginx/arb-radar.error.log\n"
            "maxretry = 5\n"
            "findtime = 10m\n"
            "bantime  = 1h\n"
            "\n"
            "[nginx-bad-request]\n"
            "enabled = true\n"
            "port    = http,https\n"
            "logpath = /var/log/nginx/access.log /var/log/nginx/arb-radar.access.log\n"
            "maxretry = 10\n"
            "findtime = 1m\n"
            "bantime  = 1h\n"
        )
        _put_text(c, nginx_jail, "/etc/fail2ban/jail.d/nginx.local", mode=0o644)
        _run(c, "systemctl restart fail2ban")
        _run(c, "fail2ban-client status")
    finally:
        c.close()
    print("\n=== Phase B done ===")


# ───────────────────────────── Phase C ──────────────────────────────


def _read_github_token() -> str:
    creds = Path("Credentials.env")
    if not creds.exists():
        raise RuntimeError("Credentials.env not found in CWD")
    for line in creds.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("GITHUB_TOKEN= not found in Credentials.env")


def _register_deploy_key(token: str, title: str, pubkey: str,
                         read_only: bool = True) -> None:
    """Register an SSH pubkey as a Deploy Key on the GitHub repo."""
    import json, urllib.request
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/keys"
    body = json.dumps({"title": title, "key": pubkey,
                       "read_only": read_only}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  GH deploy key registered: {r.status}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        if e.code == 422 and "already in use" in msg:
            print("  GH deploy key already registered (422) — ok")
            return
        raise RuntimeError(f"GH API error {e.code}: {msg}")


def phase_c_deploy_app() -> None:
    print("\n=== Phase C: deploy plan-kapkan ===")
    token = _read_github_token()

    print("[C.1] generate deploy SSH key on the VPS (under user 'arb')")
    c = _connect_key(APP_USER)
    try:
        _run(c, "mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        # generate key only if missing — idempotent
        _run(c, "test -f ~/.ssh/deploy_key || "
                "ssh-keygen -t ed25519 -N '' -C arb@plan-kapkan-vps "
                "-f ~/.ssh/deploy_key")
        _run(c, "ssh-keyscan -t rsa,ed25519 github.com 2>/dev/null "
                ">> ~/.ssh/known_hosts && sort -u -o ~/.ssh/known_hosts "
                "~/.ssh/known_hosts")
        ssh_conf = (
            "Host github.com\n"
            "    HostName github.com\n"
            "    User git\n"
            "    IdentityFile ~/.ssh/deploy_key\n"
            "    IdentitiesOnly yes\n"
        )
        _put_text(c, ssh_conf, f"/home/{APP_USER}/.ssh/config", mode=0o600)
        _run(c, "chown arb:arb ~/.ssh/config ~/.ssh/known_hosts "
                "~/.ssh/deploy_key ~/.ssh/deploy_key.pub")

        rc, deploy_pub, _ = _run(c, "cat ~/.ssh/deploy_key.pub", quiet=True)
        deploy_pub = deploy_pub.strip()
    finally:
        c.close()

    print("[C.2] register deploy key in GitHub (read-only)")
    _register_deploy_key(token, "plan-kapkan-vps-77.91.97.22", deploy_pub,
                         read_only=True)

    print("[C.3] git clone repo into /home/arb/plan-kapkan")
    c = _connect_key(APP_USER)
    try:
        # Idempotent: clone if missing, otherwise pull
        rc, out, _ = _run(c, f"test -d {APP_DIR}/.git && echo exists || echo missing",
                          quiet=True)
        if "exists" in out:
            _run(c, f"cd {APP_DIR} && git fetch origin && "
                    f"git reset --hard origin/main")
        else:
            _run(c, f"git clone {REPO_GIT_URL} {APP_DIR}")
        _run(c, f"ls {APP_DIR}")

        print("\n[C.4] write minimal Credentials.env on the VPS (DRY_RUN=1)")
        # No private keys yet — service runs in dry-run.  When user
        # adds wallet keys later, they edit this file directly on the VPS.
        env_text = (
            "# plan-kapkan VPS env — managed by deploy/vps_setup.py\n"
            "# Phase 5 graduation gate not crossed yet → DRY_RUN must stay 1.\n"
            "DRY_RUN=1\n"
            "WALLET_BACKEND=local\n"
            "# Add later when graduating: BOTn_PRIVATE_KEY=, POLY_API_KEY=, etc.\n"
        )
        _put_text(c, env_text, f"{APP_DIR}/Credentials.env", mode=0o600)
        _run(c, f"chown {APP_USER}:{APP_USER} {APP_DIR}/Credentials.env")

        print("\n[C.5] docker compose build + up -d")
        # 'arb' is in 'docker' group but the current SSH session inherited
        # groups before that; force a fresh group via 'sg docker -c'.
        _run(c, f"cd {APP_DIR} && sg docker -c 'docker compose build'",
             quiet=False)
        _run(c, f"cd {APP_DIR} && sg docker -c 'docker compose up -d'")
        time.sleep(5)
        _run(c, f"cd {APP_DIR} && sg docker -c 'docker compose ps'")

        print("\n[C.6] smoke test http://127.0.0.1:5050/api/risk_status")
        # Give the radar a few seconds to come up
        for attempt in range(6):
            rc, out, _ = _run(c, "curl -fsS http://127.0.0.1:5050/api/risk_status "
                                  "|| echo NOT_READY", check=False, quiet=True)
            if "NOT_READY" not in out and out.strip():
                print(f"  [smoke] backend up: {out.strip()[:200]}")
                break
            print(f"  [smoke] attempt {attempt+1}/6 — backend not ready, sleep 5s")
            time.sleep(5)
        else:
            print("  WARN: backend did not respond — check `docker compose logs`")
    finally:
        c.close()
    print("\n=== Phase C done ===")


# ───────────────────────────── entry ────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=("a", "b", "c", "all"), default="all")
    p.add_argument("--basic-pass", default=None,
                   help="basic auth pass for nginx; auto-generated if omitted")
    args = p.parse_args()

    root_pass = os.environ.get("DEPLOY_PASS")
    if args.phase in ("a", "all") and not root_pass:
        print("ERROR: set DEPLOY_PASS env var with the VPS root password "
              "(needed for phase A only)")
        return 2

    basic_pass = args.basic_pass or _gen_password(20)

    try:
        if args.phase in ("a", "all"):
            phase_a_hardening(root_pass)
        if args.phase in ("b", "all"):
            phase_b_service_infra(basic_pass)
            print("\n────────────────────────────────────────────────────")
            print(f"  nginx basic auth — user: {NGINX_BASIC_USER}")
            print(f"  nginx basic auth — pass: {basic_pass}")
            print("  ^ SAVE THIS NOW. The script does not store it.")
            print("────────────────────────────────────────────────────")
        if args.phase in ("c", "all"):
            phase_c_deploy_app()
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
