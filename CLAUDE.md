# CLAUDE.md

Installer and management tools for a self-hosted mail server on Ubuntu Server
24.04 and 26.04 LTS (Postfix, Dovecot, Rspamd, Roundcube, Radicale), installed
directly on the machine without Docker. Nothing here runs on a developer
machine: `install.sh` configures a real server, and the other tools run on it.

## Layout

- `install.sh`: idempotent installer. Safe to re-run; keeps mailboxes, keys and passwords.
- `templates/`: service configs with `{{VAR}}` placeholders, filled by `render` in `install.sh`.
- `bin/mailctl`: the management CLI (Python, stdlib only), installed to `/usr/local/sbin`.
- `dashboard/`: Flask admin and self-service UI, run by gunicorn as the unprivileged `mail-dashboard` user.
- `bin/mail-mcp`: MCP server (stdio JSON-RPC) that exposes `mailctl` to AI assistants over SSH.
- `bin/mail-backup`, `bin/mail-dmarc-ingest`, `bin/mail-server-fix-packages`: helpers.

## Architecture rules

- **`mailctl` is the only privileged entry point.** The dashboard and `mail-mcp`
  run `sudo mailctl --json --actor <who> ...` and never touch config files
  themselves. Add a feature in this order:
  1. In `mailctl`: a function that validates its input and returns a dict, a
     branch in `main()`, a `USAGE` line and a human-readable printer.
  2. Then the dashboard route and template, and the `mail-mcp` tool.
- `mailctl` raises `Fail("lowercase message")` for user errors. The message is
  shown to users as-is, so write it for them.
- Call `audit()` on every change.
- Secrets go in on **stdin**, never argv: `/proc/*/cmdline` is world-readable.
- `mailctl` takes a global `flock`. Long jobs release it (`backup`,
  `user import run`); `user checkpw` never takes it, so sign-ins don't wait.
- `mailctl` option parsing scans *all* arguments. Never pass a user-supplied
  value that starts with `-` (`mail-mcp` rejects them for this reason).
- Settings in `mail-server.conf` for relay, sending limit, alerts and DNS
  provider are applied on the first install only. After that the dashboard owns
  them in `/etc/mail-server/settings.json`.
- Only Ubuntu packages: Python stdlib plus `python3-flask`, `python3-pyotp`
  and `python3-qrcode`. No pip, no Docker, no other downloads.
- Dashboard pages run under a strict CSP: no inline scripts or styles. Every
  POST carries the `csrf` field. Form handlers use `@action`, which flashes the
  string they return.

## Dovecot 2.3 (24.04) vs 2.4 (26.04)

There are two templates, `templates/dovecot/dovecot-2.3.conf` and
`dovecot-2.4.conf`. Every Dovecot change goes into both and must be tested on
both. Known differences:

- Hash of a variable: `%{sha256:password}` (2.3) vs `%{password | sha256}` (2.4).
- Checking an IMAP client certificate: `imapc_ssl_verify = yes` (2.3) vs
  `ssl_client_require_valid_cert = yes` (2.4).
- In 2.4, `dovecot_config_version` must be the first line of any config file,
  even one that only has an `!include`.
- 2.4 expands `%{...}` inside `imapc_password`, and no escaping works for every
  value. `mailctl` refuses such passwords for imports.
- passwd-file: 2.4 has no `username_format`. Variables in the path work on both.
- Dovecot re-reads a passwd-file at most once per second. `write_users` pushes
  the mtime forward, and tests must wait more than 1 s after a change.

## How features work (non-obvious parts)

- **App passwords:** the source of truth is `/etc/mail-server/app-passwords.json`
  (hashes only). Dovecot reads one file per password,
  `/etc/dovecot/app-passwords/<sha256>`, through a second passdb whose path is
  the hash of the typed password. "Required" adds
  `allow_nets=local,127.0.0.0/8,::1,<server IPs>` to the users-file line, so
  the mailbox password only works from webmail, the dashboard and doveadm. The
  dashboard checks passwords with `mailctl user checkpw` (mailbox password
  only), and calendars with `--app`.
- **Mail import:** `doveadm sync -1 -R` from `imapc:` using a temporary
  root-only config in `/run/mail-server`. It runs under `systemd-run`, skips
  `\All \Flagged \Junk \Trash`, and afterwards merges remote `\Sent`/`\Drafts`
  folders into ours, de-duplicated by Message-ID.
- **DANE:** needs a DNSSEC-validating system resolver. `setup_resolver` in
  `install.sh` makes Unbound the resolver in `/etc/resolv.conf` (with
  `trust-ad`) once it sees the `ad` flag. Postfix then gets
  `smtp_dns_support_level = dnssec` and `smtp_tls_security_level = dane`
  (`encrypt` when a relay is set).

## Checking changes

There is no automated test suite. Before committing, at least run:

```bash
python3 -m py_compile bin/mailctl bin/mail-mcp dashboard/app.py && rm -rf bin/__pycache__ dashboard/__pycache__
bash -n install.sh && shellcheck install.sh bin/mail-backup   # existing SC2016/SC2153 notes are intended
```

To test against real Dovecot/Postfix without root or Docker: unpack an
`ubuntu-base` 24.04 or 26.04 tarball, then `unshare -r -m -p -f` and `chroot`
into it.

- Install packages with `apt` using `APT::Sandbox::User "root"`. Their
  maintainer scripts fail, but the binaries get unpacked.
- Add an `LD_PRELOAD` shim (in `/etc/ld.so.preload` inside the rootfs) that
  fakes `setuid`/`setgid`/`chown`.
- Use ports above 1024, and start daemons with `</dev/null >/dev/null 2>&1`,
  or their inherited pipes hang the shell.
- `doveadm auth test -x rip=<ip> user pass` simulates a remote login.

The final check is always a fresh VPS: `install.sh`, then the README's test
checklist.

## Writing

README, DNS.md and UI text are for non-experts: short sentences, plain words,
and say what a setting does for the user. Keep the README comparison table
honest; only claim what the other projects' docs show. Commit messages are a
plain summary line ("Add app passwords, mail import and DANE").
`mail-server.conf` is gitignored because it holds passwords. Never commit
`__pycache__`.
