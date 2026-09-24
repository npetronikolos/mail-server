require ["fileinto", "mailbox"];

# Rspamd adds "X-Spam: Yes" to messages that score above the add_header limit.
if header :contains "X-Spam" "Yes" {
  fileinto :create "Junk";
  stop;
}
