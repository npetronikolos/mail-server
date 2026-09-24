<?php
// Managed by mail-server install.sh -- re-running the installer overwrites it.
$config = [];
include("/etc/roundcube/debian-db-roundcube.php");

// Connect over TLS to this server's own IMAP/SMTP, checking the certificate
// against the public hostname (not checked with a self-signed certificate).
$tls = ['ssl' => ['peer_name' => '{{MAIL_HOSTNAME}}', 'verify_peer' => {{ROUNDCUBE_VERIFY_TLS}}, 'verify_peer_name' => {{ROUNDCUBE_VERIFY_TLS}}]];

$config['imap_host'] = 'ssl://127.0.0.1:993';
$config['imap_conn_options'] = $tls;
$config['smtp_host'] = 'ssl://127.0.0.1:465';
$config['smtp_conn_options'] = $tls;
$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';

$config['managesieve_host'] = 'tls://127.0.0.1:4190';
$config['managesieve_conn_options'] = $tls;
$config['managesieve_vacation'] = 1;

$config['product_name'] = '{{DOMAIN}} Webmail';
$config['des_key'] = '{{ROUNDCUBE_DES_KEY}}';
$config['username_domain'] = '{{DOMAIN}}';
$config['login_lc'] = 2;
$config['skin'] = 'elastic';
$config['support_url'] = '';
$config['enable_spellcheck'] = false;
$config['max_message_size'] = '{{MESSAGE_SIZE_LIMIT_MB}}M';
$config['use_https'] = true;
$config['session_lifetime'] = 60;
$config['ip_check'] = true;
$config['login_rate_limit'] = 3;
$config['log_logins'] = true;
$config['drafts_mbox'] = 'Drafts';
$config['junk_mbox'] = 'Junk';
$config['sent_mbox'] = 'Sent';
$config['trash_mbox'] = 'Trash';

$config['plugins'] = ['archive', 'zipdownload', 'managesieve', 'markasjunk', 'newmail_notifier'];
$config['archive_mbox'] = 'Archive';
$config['markasjunk_learning_driver'] = null;
