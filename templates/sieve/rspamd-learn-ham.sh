#!/bin/sh
# Called by Dovecot when a message is moved out of Junk.
exec /usr/bin/rspamc -h 127.0.0.1:11334 learn_ham
