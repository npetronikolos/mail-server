# Managed by mail-server install.sh
# Use the local Unbound resolver: public resolvers (Google, Cloudflare, ...)
# are blocked by DNS blocklists such as Spamhaus.
dns {
  nameserver = ["127.0.0.1:53"];
}
