# Incus Console settings for this installation.
#
# Every setting and its default is listed in /opt/incus-console/app/config.py;
# put the ones to change here and restart: systemctl restart incus-console

# The database, the secret key and the console's client certificate.
DATA_DIR = "/var/lib/incus-console"

# nginx serves HTTPS in front of the console (incus-console-setup sets it up).
BEHIND_PROXY = True
