# mail-server

A complete email server for **Ubuntu Server 24.04 LTS and 26.04 LTS**, installed
directly on the machine with one script. It uses no Docker and no control panel.

| Part | What it does |
| --- | --- |
| **Postfix** | Receives mail from the internet (port 25) and sends mail for your users (ports 587 and 465) |
| **Dovecot** | Stores mailboxes and serves them to mail apps over IMAP (port 993); runs server-side filters (Sieve) |
| **Rspamd** + Redis | Spam filtering, greylisting, DKIM signing and ARC. Learns from what users move in or out of Junk |
| **ClamAV** | Virus scanning (optional) |
| **Unbound** | Local DNS resolver, so spam blocklists such as Spamhaus work |
| **Roundcube** | Webmail at `https://mail.yourdomain.com/`, with filters and vacation replies |
| **Let's Encrypt** | Free TLS certificates, renewed automatically |
| **Fail2ban** + UFW | Blocks password-guessing attacks and closes every port the server doesn't use |
| **mailctl** | One command to manage mailboxes, aliases, domains and DNS records |

Also included: nightly backups, automatic security updates, automatic setup
for Thunderbird and other mail apps, and multiple domains on one server.

---

## Before you start

You need:

1. **A server running a fresh Ubuntu Server 24.04 or 26.04**, with at least
   2 GB RAM (4 GB if you want virus scanning) and a static public IP address.
2. **Port 25 open, both in and out.** Many cloud providers block it by default.
   Ask your provider to unblock it.
3. **A domain you control**, for example `example.com`.
4. **Access to reverse DNS (PTR)** for the server's IP. You usually set this in
   your hosting provider's control panel.

## Installation

### 1. Create the first DNS records

At your DNS provider, create these two records and wait a few minutes:

| Type | Name | Value |
| --- | --- | --- |
| A | `mail.example.com` | your server's IPv4 address |
| MX | `example.com` | `10 mail.example.com` |

At your **hosting provider**, set the reverse DNS (PTR) for the server's IP to
`mail.example.com`.

Check it from your own computer:

```bash
dig +short mail.example.com        # should print your server IP
dig +short -x YOUR.SERVER.IP       # should print mail.example.com.
```

### 2. Get the files onto the server

```bash
ssh root@YOUR.SERVER.IP
apt update && apt install -y git
git clone https://github.com/npetronikolos/mail-server.git
cd mail-server
```

### 3. Fill in the settings

```bash
cp mail-server.conf.example mail-server.conf
nano mail-server.conf
```

At minimum, set `DOMAIN`, `MAIL_HOSTNAME`, `SERVER_IPV4`, `ADMIN_EMAIL` and
`LETSENCRYPT_EMAIL`. Every setting is explained in the file.

### 4. Run the installer

```bash
sudo ./install.sh
```

It takes about 5 to 10 minutes. It asks you to choose a password for the
admin mailbox (unless you put one in the config file). At the end it prints:

- your webmail address,
- the settings to use in mail apps,
- **the full list of DNS records to create**.

You can run `install.sh` again at any time, for example after changing
`mail-server.conf`. It keeps your mailboxes, aliases, passwords and keys.

### 5. Create the remaining DNS records

Add the SPF, DKIM, DMARC and SRV records the installer printed. See
**[DNS.md](DNS.md)** for what each one does. To print them again later:

```bash
sudo mailctl dns
```

### 6. Test

Work through the [test checklist](#test-checklist) below.

---

## Everyday management: `mailctl`

Run everything with `sudo`.

```bash
# Mailboxes
mailctl user add anna@example.com             # asks for a password
mailctl user passwd anna@example.com          # change a password
mailctl user list                             # list mailboxes and their sizes
mailctl user del anna@example.com             # remove (keeps the mail on disk)
mailctl user del anna@example.com --purge     # remove and delete all the mail

# Aliases and forwarding
mailctl alias add info@example.com anna@example.com
mailctl alias add sales@example.com anna@example.com,bob@example.com
mailctl alias add team@example.com anna@example.com,someone@gmail.com
mailctl alias add @example.com anna@example.com   # catch-all: any unknown address
mailctl alias list
mailctl alias del info@example.com

# More domains on the same server
mailctl domain add example.org                # also prints its DNS records
mailctl domain list
mailctl domain del example.org

# Other
mailctl dns [domain]                          # DNS records to create
mailctl status                                # services, certificate, queue, disk
mailctl backup                                # run a backup now
```

Notes:

- Passwords must be at least 10 characters.
- A user can send email **as their own address and as any alias that
  delivers to them**. Any other From: address is refused. The owner of a
  catch-all can send as any address in that domain.
- `postmaster@` and `abuse@` are created for every domain and go to the admin.
- Password changes take effect within one second. There is nothing to restart.

## Mail app settings

| | Server | Port | Security |
| --- | --- | --- | --- |
| Incoming (IMAP) | `mail.example.com` | 993 | SSL/TLS |
| Outgoing (SMTP) | `mail.example.com` | 465 | SSL/TLS |
| Outgoing (alternative) | `mail.example.com` | 587 | STARTTLS |

The username is the **full email address**. Thunderbird, and apps that
support SRV records, fill all of this in automatically.

## Webmail

Open `https://mail.example.com/` and log in with your full email address, or
just the part before the @ for addresses at your main domain.
Under **Settings → Filters** you can create mail rules and vacation
auto-replies. They run on the server, so they also work when no app is open.

## Spam

- Mail with a score of 15 or more is **rejected**.
- Mail with a score of 6 or more is **delivered to Junk**.
- Mail with a score between 4 and 6 is **greylisted**: the sender is asked to
  retry a few minutes later. Real mail servers do; most spam bots don't.
- **Training:** moving a message into Junk teaches the filter "this is spam".
  Moving it out of Junk teaches "this is not spam". The *Mark as junk* button
  in Roundcube does the same.
- Junk is emptied automatically after 30 days.

The Rspamd dashboard (statistics, message history) is only reachable from the
server itself. Open it through an SSH tunnel:

```bash
ssh -L 11334:127.0.0.1:11334 root@mail.example.com
# then browse to http://localhost:11334
# password: sudo grep RSPAMD_WEB_PASSWORD /etc/mail-server/secrets
```

## Backups

A backup runs every night at 03:30 and is kept for 14 days. You can change
both in `mail-server.conf`. Backups go to `/var/backups/mail-server/` and
contain all mailboxes, configuration, DKIM keys, certificates and webmail
settings.

**Copy them off the server regularly.** A backup on the same disk won't help
if the server is lost. For example, from your own computer:

```bash
rsync -av root@mail.example.com:/var/backups/mail-server/ ./mail-backups/
```

### Restoring onto a new server

1. Install a fresh server with the same `mail-server.conf` and run `install.sh`.
2. Copy the backup over, then:

```bash
sudo systemctl stop postfix dovecot
sudo tar -xzf mail-backup-DATE.tar.gz -C /
sudo ./install.sh              # re-applies configuration and restarts everything
```

## Test checklist

- [ ] `sudo mailctl status`: every service shows `active`.
- [ ] Log in to webmail at `https://mail.example.com/`.
- [ ] Send a mail **from Gmail to your address**. It arrives in the inbox.
- [ ] **Reply from your address to Gmail.** It arrives in the Gmail inbox, not
      spam. In Gmail, open *⋮ → Show original*: SPF, DKIM and DMARC should
      all say `PASS`.
- [ ] Send a mail to the address shown on <https://www.mail-tester.com> and
      aim for 10/10.
- [ ] Check TLS: <https://www.checktls.com/TestReceiver> or
      <https://internet.nl/test-mail/>.
- [ ] Set up the account on your phone (IMAP 993, SMTP 465) and send and
      receive a message.
- [ ] Make sure the server is not an open relay (for example
      <https://mxtoolbox.com/diagnostic.aspx>).

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Installer says the DNS name doesn't resolve | The A record for `MAIL_HOSTNAME` is missing or hasn't propagated. Wait and check with `dig +short mail.example.com`. |
| Let's Encrypt fails | Port 80 must be reachable from the internet, and the A record must point to this server. |
| Can't send to Gmail or Outlook, or mail lands in their spam | Check the PTR record, SPF, DKIM and DMARC (`mailctl dns`), and test with mail-tester.com. A brand-new IP needs a few days of normal sending to build a reputation. |
| Outgoing mail stays in the queue | `sudo postqueue -p` shows why. "Connection timed out" to port 25 means your provider blocks outgoing port 25. |
| A user can't log in | `sudo doveadm auth test user@example.com` checks the password. Fail2ban may have blocked their IP: `sudo fail2ban-client status dovecot`, then unblock it with `sudo fail2ban-client unban 1.2.3.4`. |
| See what is happening | `sudo journalctl -u postfix -u dovecot -f` (live log), and `/var/log/rspamd/rspamd.log` for spam decisions. |

## Files and locations

| Path | Contents |
| --- | --- |
| `/etc/mail-server/mail-server.conf` | Your settings (copied in by the installer) |
| `/etc/mail-server/secrets` | Generated passwords (Rspamd web UI, Roundcube key) |
| `/var/vmail/<domain>/<user>/Maildir` | The mail itself |
| `/etc/dovecot/users` | Mailboxes and password hashes |
| `/etc/postfix/vdomains`, `vmailbox`, `virtual` | Domains, mailboxes, aliases (edit them with `mailctl`) |
| `/var/lib/rspamd/dkim/` | DKIM private keys |
| `/var/backups/mail-server/` | Nightly backups |
| `/var/log/mail-server-install.log` | Installer output |

## Repository layout

```
install.sh                 the installer
mail-server.conf.example   settings template
bin/mailctl                management command (installed to /usr/local/sbin)
bin/mail-backup            backup script (runs nightly from cron)
bin/mail-server-fix-roundcube
                           keeps Roundcube working on PHP 8.5 (Ubuntu 26.04)
templates/                 configuration files for every service
DNS.md                     DNS records explained
```
