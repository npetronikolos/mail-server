# Managed by mail-server install.sh
# Web UI: only reachable from the server itself. Open it through an SSH tunnel:
#   ssh -L 11334:127.0.0.1:11334 you@{{MAIL_HOSTNAME}}   then browse http://localhost:11334
bind_socket = "127.0.0.1:11334";
password = "{{RSPAMD_PASSWORD_HASH}}";
