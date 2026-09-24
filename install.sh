#!/usr/bin/env bash
# Variables are used indirectly by render() through {{NAME}} placeholders.
# shellcheck disable=SC2034
# -----------------------------------------------------------------------------
# Full mail server installer for Ubuntu Server 24.04 / 26.04 LTS
#
#   Postfix   - SMTP: receives mail (25) and sends for your users (587, 465)
#   Dovecot   - IMAP (993), mailbox storage, Sieve filters, ManageSieve
#   Rspamd    - spam filtering, DKIM signing, ARC, greylisting (+ Redis)
#   ClamAV    - virus scanning (optional)
#   Unbound   - local DNS resolver so DNS blocklists work
#   Roundcube - webmail at https://MAIL_HOSTNAME/ (nginx + PHP-FPM)
#   Certbot   - Let's Encrypt certificates, renewed automatically
#   Fail2ban, UFW firewall, unattended security upgrades, nightly backups
#
# Usage:
#   cp mail-server.conf.example mail-server.conf   # then edit it
#   sudo ./install.sh [path/to/mail-server.conf]
#
# Safe to run again: it re-applies the configuration and keeps existing
# mailboxes, aliases, keys and passwords.
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TEMPLATES="$SCRIPT_DIR/templates"
CONF_SRC=${1:-$SCRIPT_DIR/mail-server.conf}
ETC=/etc/mail-server
SECRETS=$ETC/secrets
LOG=/var/log/mail-server-install.log

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# ---------------------------------------------------------------- output

if [[ -t 1 ]]; then
  C_BLUE=$'\e[1;34m'; C_YEL=$'\e[1;33m'; C_RED=$'\e[1;31m'; C_GRN=$'\e[1;32m'; C_OFF=$'\e[0m'
else
  C_BLUE=; C_YEL=; C_RED=; C_GRN=; C_OFF=
fi
step() { echo; echo "${C_BLUE}==> $*${C_OFF}"; }
warn() { echo "${C_YEL}WARNING: $*${C_OFF}" >&2; }
die()  { echo "${C_RED}ERROR: $*${C_OFF}" >&2; exit 1; }

# render <template> <destination> [mode] [owner]
# Replaces {{NAME}} with the value of shell variable NAME.
render() {
  local src=$1 dst=$2 mode=${3:-644} owner=${4:-root:root} content var
  content=$(<"$src")
  for var in $(grep -o '{{[A-Z0-9_]*}}' "$src" | sort -u | tr -d '{}'); do
    [[ -v $var ]] || die "template $src uses {{$var}} but it is not set"
    content=${content//"{{$var}}"/"${!var}"}
  done
  install -D -m "$mode" -o "${owner%:*}" -g "${owner#*:}" /dev/null "$dst"
  printf '%s\n' "$content" >"$dst"
}

# get_secret NAME GENERATOR... : read NAME from the secrets file, creating it once.
get_secret() {
  local name=$1; shift
  local value
  value=$(grep -E "^$name=" "$SECRETS" 2>/dev/null | head -n1 | cut -d= -f2- || true)
  if [[ -z $value ]]; then
    value=$("$@")
    echo "$name=$value" >>"$SECRETS"
  fi
  printf '%s' "$value"
}

random_string() {
  local s
  s=$(openssl rand -base64 96 | tr -dc 'A-Za-z0-9')
  printf '%s' "${s:0:${1:-32}}"
}

# has_key <file> <key>: true if a line's first field (space or ':' separated) is key.
has_key() {
  [[ -f $1 ]] && awk -F'[ :]' -v k="$2" '$1 == k { f = 1 } END { exit !f }' "$1"
}

# wait_port <port> <name>: wait up to 60s for a local TCP port to accept connections.
wait_port() {
  local i
  for ((i = 0; i < 60; i++)); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then return 0; fi
    sleep 1
  done
  die "$2 did not start (nothing listening on port $1). See: journalctl -u $2"
}

apt_install() {
  apt-get install -y -q --no-install-recommends \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
}

# ---------------------------------------------------------------- preflight

preflight() {
  [[ $EUID -eq 0 ]] || die "run as root: sudo $0"
  [[ -r $CONF_SRC ]] || die "config not found: $CONF_SRC
  Copy mail-server.conf.example to mail-server.conf and fill it in."

  # shellcheck source=/dev/null
  source "$CONF_SRC"

  local v
  for v in DOMAIN MAIL_HOSTNAME SERVER_IPV4 ADMIN_EMAIL; do
    [[ -n ${!v:-} ]] || die "$v is not set in $CONF_SRC"
  done
  DOMAIN=${DOMAIN,,}
  MAIL_HOSTNAME=${MAIL_HOSTNAME,,}
  ADMIN_EMAIL=${ADMIN_EMAIL,,}
  SERVER_IPV6=${SERVER_IPV6:-}
  ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
  LETSENCRYPT_EMAIL=${LETSENCRYPT_EMAIL:-$ADMIN_EMAIL}
  CERT_MODE=${CERT_MODE:-letsencrypt}
  ENABLE_AUTOCONFIG_SUBDOMAIN=${ENABLE_AUTOCONFIG_SUBDOMAIN:-no}
  ENABLE_CLAMAV=${ENABLE_CLAMAV:-yes}
  ENABLE_WEBMAIL=${ENABLE_WEBMAIL:-yes}
  ENABLE_FIREWALL=${ENABLE_FIREWALL:-yes}
  SSH_PORT=${SSH_PORT:-22}
  DKIM_SELECTOR=${DKIM_SELECTOR:-mail}
  MESSAGE_SIZE_LIMIT_MB=${MESSAGE_SIZE_LIMIT_MB:-50}
  TIMEZONE=${TIMEZONE:-UTC}
  BACKUP_DIR=${BACKUP_DIR:-/var/backups/mail-server}
  BACKUP_KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}

  [[ $DOMAIN == example.com ]] && die "edit $CONF_SRC first - DOMAIN is still example.com"
  [[ $ADMIN_EMAIL == *@"$DOMAIN" ]] || die "ADMIN_EMAIL must be an address at $DOMAIN"
  [[ $MAIL_HOSTNAME == *.* ]] || die "MAIL_HOSTNAME must be a full name like mail.$DOMAIN"
  [[ $SERVER_IPV4 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "SERVER_IPV4 is not an IPv4 address"
  [[ $CERT_MODE == letsencrypt || $CERT_MODE == selfsigned ]] || die "CERT_MODE must be letsencrypt or selfsigned"
  [[ $MESSAGE_SIZE_LIMIT_MB =~ ^[0-9]+$ ]] || die "MESSAGE_SIZE_LIMIT_MB must be a number"
  [[ $SSH_PORT =~ ^[0-9]+$ ]] || die "SSH_PORT must be a number"

  # shellcheck source=/dev/null
  source /etc/os-release
  if [[ ${ID:-} != ubuntu ]]; then
    die "this installer supports Ubuntu Server only (found ${PRETTY_NAME:-unknown})"
  fi
  if dpkg --compare-versions "${VERSION_ID:-0}" lt 24.04; then
    die "Ubuntu 24.04 or newer is required (found $VERSION_ID)"
  fi

  local mem_mb
  mem_mb=$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)
  if [[ $ENABLE_CLAMAV == yes && $mem_mb -lt 2800 ]]; then
    warn "only ${mem_mb} MB RAM: ClamAV needs about 1.5 GB. Consider ENABLE_CLAMAV=\"no\" or adding swap."
  fi

  if [[ $CERT_MODE == letsencrypt && ${SKIP_DNS_CHECK:-no} != yes ]]; then
    local resolved
    resolved=$(getent ahostsv4 "$MAIL_HOSTNAME" | awk 'NR == 1 { print $1 }' || true)
    if [[ $resolved != "$SERVER_IPV4" ]]; then
      die "$MAIL_HOSTNAME resolves to '${resolved:-nothing}', not $SERVER_IPV4.
  Create the DNS A record first (see DNS.md), wait a few minutes, and run again.
  (Set SKIP_DNS_CHECK=yes to bypass this check.)"
    fi
  fi

  if ! timeout 8 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
    warn "could not connect OUT on port 25. Your provider may block it; you will not be able to
         send mail to other servers until they unblock it."
  fi

  INET_PROTOCOLS=ipv4
  DOVECOT_LISTEN="*"
  UNBOUND_IPV6=no
  if [[ -n $SERVER_IPV6 ]]; then
    INET_PROTOCOLS=all
    DOVECOT_LISTEN="*, ::"
    UNBOUND_IPV6=yes
  fi
  # nginx: listen on IPv6 too, unless IPv6 is switched off on this machine.
  NGINX_LISTEN_V6_HTTP="    # (IPv6 disabled on this machine)"
  NGINX_LISTEN_V6_HTTPS=$NGINX_LISTEN_V6_HTTP
  if [[ -s /proc/net/if_inet6 ]]; then
    NGINX_LISTEN_V6_HTTP="    listen [::]:80;"
    NGINX_LISTEN_V6_HTTPS="    listen [::]:443 ssl;"
  fi
  MESSAGE_SIZE_LIMIT_BYTES=$((MESSAGE_SIZE_LIMIT_MB * 1024 * 1024))
  # Attachments grow by about a third when encoded for email.
  WEB_UPLOAD_LIMIT_MB=$((MESSAGE_SIZE_LIMIT_MB * 4 / 3 + 1))
  WEB_SERVER_NAMES=$MAIL_HOSTNAME
  if [[ $ENABLE_AUTOCONFIG_SUBDOMAIN == yes ]]; then
    WEB_SERVER_NAMES="$MAIL_HOSTNAME autoconfig.$DOMAIN"
  fi
}

# ---------------------------------------------------------------- steps

setup_system() {
  step "Basic system settings"
  install -d -m 755 "$ETC"
  install -m 640 "$CONF_SRC" "$ETC/mail-server.conf"
  touch "$SECRETS"
  chmod 600 "$SECRETS"

  timedatectl set-timezone "$TIMEZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime

  hostnamectl set-hostname "$MAIL_HOSTNAME" 2>/dev/null || echo "$MAIL_HOSTNAME" >/etc/hostname
  if ! grep -qE "^[^#]*[[:space:]]$MAIL_HOSTNAME([[:space:]]|$)" /etc/hosts; then
    echo "127.0.1.1 $MAIL_HOSTNAME ${MAIL_HOSTNAME%%.*}" >>/etc/hosts
  fi
  # Stop cloud-init from resetting the hostname on reboot.
  if [[ -d /etc/cloud/cloud.cfg.d ]]; then
    echo "preserve_hostname: true" >/etc/cloud/cloud.cfg.d/99-mail-server.cfg
  fi
  echo "$MAIL_HOSTNAME" >/etc/mailname
}

install_packages() {
  step "Installing packages (this takes a few minutes)"
  debconf-set-selections <<EOF
postfix postfix/main_mailer_type select Internet Site
postfix postfix/mailname string $MAIL_HOSTNAME
roundcube-core roundcube/dbconfig-install boolean true
roundcube-core roundcube/database-type select sqlite3
roundcube-core roundcube/restart-webserver multiselect
roundcube-core roundcube/reconfigure-webserver multiselect
EOF
  apt-get update -q

  # Don't let packages start their services with default settings while
  # installing (e.g. nginx/Dovecot fail on machines with IPv6 disabled).
  # Each service is started later, once it has been configured.
  local own_policy=no
  if [[ ! -e /usr/sbin/policy-rc.d ]]; then
    printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
    chmod 755 /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
    own_policy=yes
  fi

  apt_install ca-certificates curl openssl cron \
    postfix postfix-pcre \
    dovecot-core dovecot-imapd dovecot-lmtpd dovecot-sieve dovecot-managesieved \
    rspamd redis-server unbound dns-root-data \
    nginx certbot \
    fail2ban python3-systemd \
    unattended-upgrades

  if [[ $ENABLE_CLAMAV == yes ]]; then
    apt_install clamav-daemon clamav-freshclam
  fi
  if [[ $ENABLE_FIREWALL == yes ]]; then
    apt_install ufw
  fi
  if [[ $ENABLE_WEBMAIL == yes ]]; then
    # nginx must already be installed so Roundcube does not pull in Apache.
    apt_install php-fpm
    apt_install roundcube roundcube-core roundcube-sqlite3 roundcube-plugins
  fi
  if [[ $own_policy == yes ]]; then
    rm -f /usr/sbin/policy-rc.d
    trap - EXIT
  fi
}

setup_vmail_user() {
  step "Mail storage user"
  if ! getent group vmail >/dev/null; then
    if getent group 5000 >/dev/null; then groupadd --system vmail; else groupadd --gid 5000 vmail; fi
  fi
  if ! id vmail >/dev/null 2>&1; then
    local uid_opt=()
    getent passwd 5000 >/dev/null || uid_opt=(--uid 5000)
    useradd "${uid_opt[@]}" --gid vmail --home-dir /var/vmail --no-create-home \
      --shell /usr/sbin/nologin --comment "Virtual mail" vmail
  fi
  VMAIL_UID=$(id -u vmail)
  install -d -o vmail -g vmail -m 750 /var/vmail
}

setup_nginx_http() {
  install -d -m 755 /var/www/letsencrypt /var/www/mail-autoconfig
  rm -f /etc/nginx/sites-enabled/default
  render "$TEMPLATES/nginx/mail-http.conf" /etc/nginx/sites-available/mail-server
  ln -sf ../sites-available/mail-server /etc/nginx/sites-enabled/mail-server
  nginx -t -q
  systemctl enable --now nginx >/dev/null
  systemctl reload nginx
}

setup_certificates() {
  step "TLS certificate ($CERT_MODE)"
  setup_nginx_http

  if [[ $CERT_MODE == selfsigned ]]; then
    TLS_CERT=$ETC/tls/fullchain.pem
    TLS_KEY=$ETC/tls/privkey.pem
    if [[ ! -f $TLS_CERT ]]; then
      install -d -m 700 "$ETC/tls"
      local san="DNS:$MAIL_HOSTNAME"
      [[ $ENABLE_AUTOCONFIG_SUBDOMAIN == yes ]] && san+=",DNS:autoconfig.$DOMAIN"
      openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -sha256 \
        -keyout "$TLS_KEY" -out "$TLS_CERT" -subj "/CN=$MAIL_HOSTNAME" \
        -addext "subjectAltName=$san" 2>/dev/null
      chmod 600 "$TLS_KEY"
    fi
    return
  fi

  TLS_CERT=/etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem
  TLS_KEY=/etc/letsencrypt/live/$MAIL_HOSTNAME/privkey.pem
  local domains=(-d "$MAIL_HOSTNAME")
  [[ $ENABLE_AUTOCONFIG_SUBDOMAIN == yes ]] && domains+=(-d "autoconfig.$DOMAIN")

  certbot certonly --webroot -w /var/www/letsencrypt \
    --cert-name "$MAIL_HOSTNAME" "${domains[@]}" \
    --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email \
    --non-interactive --keep-until-expiring --expand \
    || die "could not get a Let's Encrypt certificate. Check that $MAIL_HOSTNAME points to
  this server and that port 80 is reachable from the internet."

  # Reload everything that uses the certificate after each renewal.
  install -d /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/mail-server.sh <<'EOF'
#!/bin/sh
# Installed by mail-server install.sh
systemctl reload postfix dovecot nginx
EOF
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/mail-server.sh
  systemctl enable --now certbot.timer >/dev/null 2>&1 || true
}

setup_postfix() {
  step "Postfix (SMTP)"
  local f
  for f in vdomains vmailbox virtual sender_login; do
    [[ -f /etc/postfix/$f ]] || install -m 644 /dev/null "/etc/postfix/$f"
    postmap "hash:/etc/postfix/$f"
  done

  postconf -e \
    "myhostname = $MAIL_HOSTNAME" \
    "mydomain = $DOMAIN" \
    'myorigin = $mydomain' \
    "mydestination = localhost" \
    "inet_interfaces = all" \
    "inet_protocols = $INET_PROTOCOLS" \
    "mynetworks = 127.0.0.0/8 [::1]/128" \
    'smtpd_banner = $myhostname ESMTP' \
    "biff = no" \
    "append_dot_mydomain = no" \
    "compatibility_level = 3.6" \
    "recipient_delimiter = +" \
    "message_size_limit = $MESSAGE_SIZE_LIMIT_BYTES" \
    "mailbox_size_limit = 0" \
    "alias_maps = hash:/etc/aliases" \
    "alias_database = hash:/etc/aliases" \
    \
    "smtpd_tls_chain_files = $TLS_KEY, $TLS_CERT" \
    "smtpd_tls_security_level = may" \
    "smtpd_tls_auth_only = yes" \
    "smtpd_tls_protocols = >=TLSv1" \
    "smtpd_tls_mandatory_protocols = >=TLSv1.2" \
    "smtpd_tls_loglevel = 1" \
    "smtpd_tls_received_header = yes" \
    'smtpd_tls_session_cache_database = btree:${data_directory}/smtpd_scache' \
    "smtp_tls_security_level = may" \
    "smtp_tls_loglevel = 1" \
    "smtp_tls_CApath = /etc/ssl/certs" \
    'smtp_tls_session_cache_database = btree:${data_directory}/smtp_scache' \
    \
    "smtpd_sasl_type = dovecot" \
    "smtpd_sasl_path = private/auth" \
    "smtpd_sasl_auth_enable = no" \
    "smtpd_sasl_security_options = noanonymous" \
    "smtpd_sasl_tls_security_options = noanonymous" \
    \
    "virtual_mailbox_domains = hash:/etc/postfix/vdomains" \
    "virtual_mailbox_maps = hash:/etc/postfix/vmailbox" \
    "virtual_alias_maps = hash:/etc/postfix/virtual" \
    "virtual_transport = lmtp:unix:private/dovecot-lmtp" \
    "smtpd_sender_login_maps = hash:/etc/postfix/sender_login" \
    \
    "smtpd_helo_required = yes" \
    "disable_vrfy_command = yes" \
    "strict_rfc821_envelopes = yes" \
    "smtpd_helo_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_invalid_helo_hostname, reject_non_fqdn_helo_hostname" \
    "smtpd_sender_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_non_fqdn_sender, reject_unknown_sender_domain" \
    "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, defer_unauth_destination" \
    "smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_non_fqdn_recipient, reject_unknown_recipient_domain, reject_unlisted_recipient" \
    \
    "smtpd_milters = inet:127.0.0.1:11332" \
    'non_smtpd_milters = $smtpd_milters' \
    "milter_default_action = accept" \
    "milter_protocol = 6" \
    "milter_mail_macros = i {mail_addr} {client_addr} {client_name} {auth_authen}"

  # Mail client ports: 587 (STARTTLS) and 465 (implicit TLS). Login required,
  # and users may only send From: addresses they own (see sender_login map).
  local svc
  for svc in submission submissions; do
    postconf -M "$svc/inet=$svc inet n - y - - smtpd"
    postconf -P \
      "$svc/inet/syslog_name=postfix/$svc" \
      "$svc/inet/smtpd_sasl_auth_enable=yes" \
      "$svc/inet/smtpd_tls_auth_only=yes" \
      "$svc/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject" \
      "$svc/inet/smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject" \
      "$svc/inet/smtpd_relay_restrictions=permit_sasl_authenticated,reject" \
      "$svc/inet/smtpd_recipient_restrictions=permit_sasl_authenticated,reject" \
      "$svc/inet/milter_macro_daemon_name=ORIGINATING"
  done
  postconf -P "submission/inet/smtpd_tls_security_level=encrypt"
  postconf -P "submissions/inet/smtpd_tls_wrappermode=yes"

  newaliases
  postfix check
  systemctl enable postfix >/dev/null
}

setup_dovecot() {
  step "Dovecot (IMAP)"
  local version tpl
  version=$(dovecot --version | awk '{ print $1 }')
  if dpkg --compare-versions "$version" ge 2.4; then tpl=dovecot-2.4.conf; else tpl=dovecot-2.3.conf; fi

  [[ -f /etc/dovecot/dovecot.conf.orig ]] || cp /etc/dovecot/dovecot.conf /etc/dovecot/dovecot.conf.orig
  render "$TEMPLATES/dovecot/$tpl" /etc/dovecot/dovecot.conf 644

  [[ -f /etc/dovecot/users ]] || install -m 640 -o root -g dovecot /dev/null /etc/dovecot/users
  chown root:dovecot /etc/dovecot/users
  chmod 640 /etc/dovecot/users

  install -d -m 755 /etc/dovecot/sieve
  install -m 644 "$TEMPLATES"/sieve/*.sieve /etc/dovecot/sieve/
  install -m 755 "$TEMPLATES"/sieve/*.sh /etc/dovecot/sieve/
  local s
  for s in /etc/dovecot/sieve/*.sieve; do sievec "$s"; done

  doveconf -n >/dev/null
  systemctl enable dovecot >/dev/null
}

setup_rspamd() {
  step "Rspamd (spam filter + DKIM), Redis, Unbound, ClamAV"

  render "$TEMPLATES/unbound/mail-server.conf" /etc/unbound/unbound.conf.d/mail-server.conf
  # Keep /etc/resolv.conf as it is; Unbound is only for Rspamd.
  systemctl disable --now unbound-resolvconf.service >/dev/null 2>&1 || true
  systemctl enable unbound >/dev/null
  systemctl restart unbound

  systemctl enable --now redis-server >/dev/null

  local rspamd_pw
  rspamd_pw=$(get_secret RSPAMD_WEB_PASSWORD random_string 24)
  RSPAMD_PASSWORD_HASH=$(rspamadm pw -p "$rspamd_pw")

  install -d -m 755 /etc/rspamd/local.d
  local f
  for f in "$TEMPLATES"/rspamd/*; do
    render "$f" "/etc/rspamd/local.d/$(basename "$f")" 640 root:_rspamd
  done
  if [[ $ENABLE_CLAMAV == yes ]]; then
    systemctl enable clamav-freshclam clamav-daemon >/dev/null
    systemctl start clamav-freshclam || true
    # Skipped until freshclam has downloaded the virus database; see cron.d.
    systemctl start clamav-daemon 2>/dev/null || true
  else
    rm -f /etc/rspamd/local.d/antivirus.conf
  fi
  install -d -o _rspamd -g _rspamd -m 750 /var/lib/rspamd/dkim

  rspamadm configtest >/dev/null
  systemctl enable rspamd >/dev/null
  systemctl restart rspamd
}

setup_webmail() {
  install -d -m 755 /var/www/mail-autoconfig
  render "$TEMPLATES/autoconfig.xml" /var/www/mail-autoconfig/config-v1.1.xml

  if [[ $ENABLE_WEBMAIL != yes ]]; then
    WEBMAIL_LOCATIONS=$(<"$TEMPLATES/nginx/no-webmail-locations.conf")
  else
    step "Roundcube webmail"
    local php_ver
    php_ver=$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')
    PHP_FPM_SOCK=/run/php/php$php_ver-fpm.sock
    systemctl enable --now "php$php_ver-fpm" >/dev/null

    # Ubuntu 26.04 ships PHP 8.5 with a Roundcube that predates it; patch it
    # now and again after every package update.
    install -m 755 "$SCRIPT_DIR/bin/mail-server-fix-roundcube" /usr/local/sbin/mail-server-fix-roundcube
    cat >/etc/apt/apt.conf.d/99mail-server <<'EOF'
// Installed by mail-server install.sh
DPkg::Post-Invoke { "if [ -x /usr/local/sbin/mail-server-fix-roundcube ]; then /usr/local/sbin/mail-server-fix-roundcube || true; fi"; };
EOF
    /usr/local/sbin/mail-server-fix-roundcube

    ROUNDCUBE_DES_KEY=$(get_secret ROUNDCUBE_DES_KEY random_string 24)
    ROUNDCUBE_VERIFY_TLS=true
    [[ $CERT_MODE == selfsigned ]] && ROUNDCUBE_VERIFY_TLS=false
    render "$TEMPLATES/roundcube/config.inc.php" /etc/roundcube/config.inc.php 640 root:www-data

    install -d -o www-data -g adm -m 750 /var/log/roundcube
    [[ -f /var/log/roundcube/errors.log ]] || install -o www-data -g adm -m 640 /dev/null /var/log/roundcube/errors.log

    local tmp
    tmp=$(mktemp)
    render "$TEMPLATES/nginx/webmail-locations.conf" "$tmp"
    WEBMAIL_LOCATIONS=$(<"$tmp")
    rm -f "$tmp"
    systemctl reload "php$php_ver-fpm"
  fi

  step "nginx (HTTPS)"
  local tmp
  tmp=$(mktemp)
  render "$TEMPLATES/nginx/mail-https.conf" "$tmp"
  cat "$tmp" >>/etc/nginx/sites-available/mail-server
  rm -f "$tmp"
  nginx -t -q
  systemctl reload nginx
}

setup_security() {
  step "Firewall, Fail2ban, automatic security updates"

  if [[ $ENABLE_FIREWALL == yes ]]; then
    ufw allow "$SSH_PORT/tcp" comment 'SSH' >/dev/null
    ufw allow 25/tcp  comment 'SMTP'        >/dev/null
    ufw allow 465/tcp comment 'SMTPS'       >/dev/null
    ufw allow 587/tcp comment 'Submission'  >/dev/null
    ufw allow 993/tcp comment 'IMAPS'       >/dev/null
    ufw allow 80/tcp  comment 'HTTP'        >/dev/null
    ufw allow 443/tcp comment 'HTTPS'       >/dev/null
    ufw --force enable >/dev/null
    FAIL2BAN_BANACTION=ufw
  else
    FAIL2BAN_BANACTION=iptables-multiport
  fi

  FAIL2BAN_IGNORE="$SERVER_IPV4 $SERVER_IPV6"
  WEBMAIL_JAIL=false
  [[ $ENABLE_WEBMAIL == yes ]] && WEBMAIL_JAIL=true
  render "$TEMPLATES/fail2ban/mail-server.local" /etc/fail2ban/jail.d/mail-server.local
  systemctl enable fail2ban >/dev/null
  systemctl restart fail2ban

  cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
}

setup_tools() {
  step "Management tools and backups"
  install -m 755 "$SCRIPT_DIR/bin/mailctl" /usr/local/sbin/mailctl
  install -m 755 "$SCRIPT_DIR/bin/mail-backup" /usr/local/sbin/mail-backup

  cat >/etc/cron.d/mail-server <<'EOF'
# Installed by mail-server install.sh
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Nightly backup of mail, configuration and keys
30 3 * * * root /usr/local/sbin/mail-backup >/dev/null
EOF
  if [[ $ENABLE_CLAMAV == yes ]]; then
    # clamd only starts once freshclam has downloaded the virus database
    # (a few minutes after install); this starts it then, and after a crash.
    cat >>/etc/cron.d/mail-server <<'EOF'
*/5 * * * * root systemctl is-active --quiet clamav-daemon || systemctl start clamav-daemon >/dev/null 2>&1
EOF
  fi
}

start_services() {
  step "Starting services"
  systemctl restart rspamd
  systemctl restart dovecot
  systemctl restart postfix
  systemctl reload nginx
  # Postfix passes mail through unfiltered while Rspamd is still starting.
  wait_port 11332 rspamd
  wait_port 993 dovecot
  wait_port 25 postfix
}

setup_accounts() {
  step "Domain and administrator mailbox"
  if ! has_key /etc/postfix/vdomains "$DOMAIN"; then
    mailctl domain add "$DOMAIN" >/dev/null
  fi

  GENERATED_ADMIN_PASSWORD=
  if ! has_key /etc/dovecot/users "$ADMIN_EMAIL"; then
    if [[ -n $ADMIN_PASSWORD ]]; then
      printf '%s\n' "$ADMIN_PASSWORD" | mailctl user add "$ADMIN_EMAIL"
    elif [[ -t 0 ]]; then
      echo "Choose a password for $ADMIN_EMAIL (at least 10 characters):"
      mailctl user add "$ADMIN_EMAIL"
    else
      GENERATED_ADMIN_PASSWORD=$(random_string 20)
      printf '%s\n' "$GENERATED_ADMIN_PASSWORD" | mailctl user add "$ADMIN_EMAIL"
    fi
  fi

  # System mail for root goes to the administrator.
  if ! has_key /etc/postfix/virtual "root@$DOMAIN" && ! has_key /etc/dovecot/users "root@$DOMAIN"; then
    mailctl alias add "root@$DOMAIN" "$ADMIN_EMAIL" >/dev/null
  fi
}

summary() {
  echo
  echo "${C_GRN}=====================================================================${C_OFF}"
  echo "${C_GRN} Mail server installed${C_OFF}"
  echo "${C_GRN}=====================================================================${C_OFF}"
  cat <<EOF

Mailbox:     $ADMIN_EMAIL
EOF
  [[ -n $GENERATED_ADMIN_PASSWORD ]] && echo "Password:    $GENERATED_ADMIN_PASSWORD   (change it: mailctl user passwd $ADMIN_EMAIL)"
  [[ $ENABLE_WEBMAIL == yes ]] && echo "Webmail:     https://$MAIL_HOSTNAME/"
  cat <<EOF

Mail app settings (Outlook, Thunderbird, iPhone, Android):
  Username:  your full email address
  Incoming:  IMAP  $MAIL_HOSTNAME  port 993  SSL/TLS
  Outgoing:  SMTP  $MAIL_HOSTNAME  port 465  SSL/TLS   (or 587 STARTTLS)

Rspamd web UI password is in $SECRETS (open it through an SSH tunnel, see README).

EOF
  mailctl dns "$DOMAIN"
  cat <<EOF

Next steps:
  1. Create the DNS records above (see DNS.md for help).
  2. Check PTR:    dig -x $SERVER_IPV4 +short    -> should print $MAIL_HOSTNAME.
  3. Send a test mail to the address shown on https://www.mail-tester.com
  4. Manage mail:  sudo mailctl help

Installation log: $LOG
EOF
}

main() {
  [[ $EUID -eq 0 ]] || die "run as root: sudo $0"
  exec > >(tee -a "$LOG") 2>&1
  echo "---- install started $(date -Is) ----"

  preflight
  setup_system
  install_packages
  setup_vmail_user
  setup_certificates
  setup_postfix
  setup_dovecot
  setup_rspamd
  setup_webmail
  setup_tools
  start_services
  setup_accounts
  setup_security
  summary
}

main "$@"
