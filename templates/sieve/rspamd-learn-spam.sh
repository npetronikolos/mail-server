#!/bin/sh
# Called by Dovecot when a message is moved into Junk.
exec /usr/bin/rspamc -h 127.0.0.1:11334 learn_spam
