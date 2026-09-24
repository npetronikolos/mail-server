require ["vnd.dovecot.pipe", "copy", "imapsieve", "environment", "variables"];

# Deleting spam (Junk -> Trash) is not a sign that it was good mail.
if environment :matches "imap.mailbox" "*" {
  set "mailbox" "${1}";
}
if string "${mailbox}" "Trash" {
  stop;
}

if environment :matches "imap.user" "*" {
  set "username" "${1}";
}

pipe :copy "rspamd-learn-ham.sh" [ "${username}" ];
