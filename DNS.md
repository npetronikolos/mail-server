# DNS records

Other mail servers (Gmail, Outlook, Yahoo...) use DNS to decide whether to
trust mail from your server. Missing or wrong records are the most common
reason mail ends up in spam or gets rejected.

After installation, **`sudo mailctl dns` prints every record with your real
values**, including the DKIM key. The examples below use `example.com`,
`mail.example.com` and the IP `203.0.113.10`.

## Records at your DNS provider

| Type | Name | Value | Purpose |
| --- | --- | --- | --- |
| A | `mail` | `203.0.113.10` | Where the mail server is |
| AAAA | `mail` | your IPv6 | Only if you set `SERVER_IPV6` |
| MX | `@` | `10 mail.example.com.` | "Mail for @example.com goes to mail.example.com" |
| TXT | `@` | `v=spf1 mx -all` | **SPF:** only the MX server may send mail for this domain |
| TXT | `mail._domainkey` | `v=DKIM1; k=rsa; p=MIIBIj...` | **DKIM:** public key that proves your mail wasn't altered (get it from `mailctl dns`) |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:postmaster@example.com; adkim=r; aspf=r` | **DMARC:** tells receivers to treat mail failing SPF and DKIM as spam, and to send you reports |
| SRV | `_imaps._tcp` | `0 1 993 mail.example.com.` | Automatic setup in mail apps |
| SRV | `_submissions._tcp` | `0 1 465 mail.example.com.` | Automatic setup in mail apps |
| SRV | `_submission._tcp` | `0 1 587 mail.example.com.` | Automatic setup in mail apps |
| CNAME | `autoconfig` | `mail.example.com.` | Automatic setup in Thunderbird (optional, see below) |

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

**About the autoconfig record.** Thunderbird can fill in all the settings when
this record exists. Create the CNAME, then set
`ENABLE_AUTOCONFIG_SUBDOMAIN="yes"` in `mail-server.conf` and run
`sudo ./install.sh` again, so the certificate also covers
`autoconfig.example.com`.

**Additional domains.** `sudo mailctl domain add example.org` prints the records
for the new domain. It needs MX, SPF, DKIM, DMARC and the SRV records. The
A and PTR records stay those of `mail.example.com`.

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
