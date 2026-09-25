# DNS records

Other mail servers (Gmail, Outlook, Yahoo...) use DNS to decide whether to
trust mail from your server. Missing or wrong records are the most common
reason mail ends up in spam or gets rejected.

After installation, the dashboard's **Domains & DNS** page lists every record
with your real values (click a value to copy it) and **checks each one live**.
From the terminal: `sudo mailctl dns` prints them and `sudo mailctl dns --check`
verifies them. The examples below use `example.com`,
`mail.example.com` and the IP `203.0.113.10`.

## Let the server create them

With **Cloudflare** or **DigitalOcean**, the server can create and update all
of these records through the provider's API:

1. Create an API token.
   - **Cloudflare:** My Profile → API Tokens → Create Token, using the "Edit zone DNS" template, limited to your domain.
   - **DigitalOcean:** API → Generate New Token, with write scope.
2. Dashboard → **Delivery & alerts → DNS automation**: choose the provider and
   paste the token. (Or set `DNS_PROVIDER` and `DNS_API_TOKEN` before
   installing.)
3. Open a domain on the **Domains & DNS** page, then **Create records at …**.
   You see every change first. Nothing is touched until you confirm.

It only manages the records in the tables below. Other records (your website,
Google/Microsoft verification TXT records...) are left alone. Two records are
replaced: the domain's MX, which moves your mail to this server, and its SPF
record. The preview warns you about both. The reverse DNS (PTR) record always
stays manual, at your hosting provider.

## Required records at your DNS provider

| Type | Name | Value | Purpose |
| --- | --- | --- | --- |
| A | `mail` | `203.0.113.10` | Where the mail server is |
| AAAA | `mail` | your IPv6 | Only if you set `SERVER_IPV6` |
| MX | `@` | `10 mail.example.com.` | "Mail for @example.com goes to mail.example.com" |
| TXT | `@` | `v=spf1 mx -all` | **SPF:** only the MX server may send mail for this domain (with a relay service, its `include:` is added, e.g. `v=spf1 mx include:spf.brevo.com -all`) |
| TXT | `mail._domainkey` | `v=DKIM1; k=rsa; p=MIIBIj...` | **DKIM:** public key that proves your mail wasn't altered (get it from `mailctl dns`) |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:dmarc-reports@example.com; adkim=r; aspf=r` | **DMARC:** tells receivers to treat mail failing SPF and DKIM as spam, and to send daily reports (shown on the dashboard's DMARC page) |

## Recommended records

These are optional, but each one adds something useful:

| Type | Name | Value | Purpose |
| --- | --- | --- | --- |
| CNAME | `mta-sts` | `mail.example.com.` | **MTA-STS:** where senders fetch your encryption policy |
| TXT | `_mta-sts` | `v=STSv1; id=20260924120000` | **MTA-STS:** tells senders a policy exists (use the id `mailctl dns` shows) |
| TXT | `_smtp._tls` | `v=TLSRPTv1; rua=mailto:postmaster@example.com` | **TLS-RPT:** daily reports if anyone has trouble sending to you securely |
| SRV | `_imaps._tcp` | `0 1 993 mail.example.com.` | Automatic setup in mail apps |
| SRV | `_submissions._tcp` | `0 1 465 mail.example.com.` | Automatic setup in mail apps |
| SRV | `_submission._tcp` | `0 1 587 mail.example.com.` | Automatic setup in mail apps |
| CNAME | `autoconfig` | `mail.example.com.` | Automatic setup in Thunderbird |
| CNAME | `autodiscover` | `mail.example.com.` | Automatic setup in Outlook |
| SRV | `_caldavs._tcp` | `0 1 443 mail.example.com.` | Automatic setup for calendars |
| SRV | `_carddavs._tcp` | `0 1 443 mail.example.com.` | Automatic setup for contacts |

`@` means the domain itself. Some providers want it left empty, or want the
full name `example.com`.

## Reverse DNS (PTR) at your hosting provider

| IP | Points to |
| --- | --- |
| `203.0.113.10` | `mail.example.com` |

You **don't** set this at your DNS provider. It lives in the control panel of
whoever gave you the IP address (Hetzner, OVH, Linode, Vultr, DigitalOcean...).
Gmail and Outlook reject or spam-folder mail from IPs without a matching PTR.

If you set `SERVER_IPV6`, set a PTR for the IPv6 address too.

## Tips

**The DKIM value is long.** TXT records are limited to 255 characters per
string. Most DNS providers split longer values automatically. If yours
complains, split the value into several quoted parts:

```
"v=DKIM1; k=rsa; p=MIIBIjANBgkqh...first part..." "...rest of the key..."
```

**Certificates for mta-sts, autoconfig and autodiscover.** Those names are
served over HTTPS, so the certificate must cover them. Once their CNAME records
exist, the server adds them to its Let's Encrypt certificate automatically
(checked daily). To do it right away, use **Refresh certificate names** on the
dashboard's Overview, or run `sudo mailctl cert`.

**About MTA-STS.** With MTA-STS, servers such as Gmail and Outlook refuse to
deliver to you over an unencrypted or forged connection. The policy is in
`enforce` mode by default (`MTA_STS_MODE` in `mail-server.conf`). Set it to
`testing` if you want reports first. The `id` in the `_mta-sts` record must
change whenever the policy changes, so always copy the current value from the
dashboard or `mailctl dns`.

**Additional domains.** `sudo mailctl domain add example.org` prints the records
for the new domain (so does the dashboard). It needs MX, SPF, DKIM and DMARC,
plus whichever recommended records you want. The A and PTR records stay those
of `mail.example.com`.

**Check your records:**

```bash
dig +short MX example.com
dig +short TXT example.com
dig +short TXT mail._domainkey.example.com
dig +short TXT _dmarc.example.com
dig +short -x 203.0.113.10
```

Online checkers: <https://mxtoolbox.com/SuperTool.aspx> and
<https://www.mail-tester.com> (send it a mail and it scores everything).

DNS changes can take anywhere from a few minutes to a few hours to be
visible everywhere.
