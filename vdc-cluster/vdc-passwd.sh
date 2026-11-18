#!/usr/bin/env bash
# vdc-passwd.sh - set the password of any account of the vdc-cluster cloud API: the provider
# (also admin itself), tenant users and auditors. Runs on the main PC, next to the kit, as root.
#
#   sudo ./vdc-passwd.sh -k KIT -l           list the accounts
#   sudo ./vdc-passwd.sh -k KIT alice        new password for alice (asked twice)
#   sudo ./vdc-passwd.sh -k KIT -r alice     a random password, printed once
#   KIT: the kit folder; without -k, $VDC_KIT, else the current folder
#
# The API keeps only scrypt hashes (no password can be read back) and has no "change password"
# call. This script writes a new hash, in the API's own format, into the API's database
# (state/cloud/cloud.db), so the account keeps its role, tenant, admin flag and creation date.
# It adds a "user.password" event to the audit log. For admin it also updates
# state/cloud/admin-password, so "cluster.sh cloud admin" shows the new one.
# Nothing in the kit itself is changed, and the API need not be stopped.
# A login the user already has stays valid until it expires (8 hours).
set -euo pipefail

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "vdc-passwd: $*" >&2; exit 1; }

KIT=${VDC_KIT:-.}
RANDOM_PW=0
LIST=0
while getopts "k:rlh" o; do
  case $o in
    k) KIT=$OPTARG ;;
    r) RANDOM_PW=1 ;;
    l) LIST=1 ;;
    h) usage 0 ;;
    *) usage 1 ;;
  esac
done
shift $((OPTIND - 1))

KIT=$(cd "$KIT" 2>/dev/null && pwd) || die "no folder $KIT"
CST=$KIT/state/cloud
DB=$CST/cloud.db
[ -f "$KIT/cluster.sh" ] || die "$KIT is not the kit folder (no cluster.sh); use -k KIT or run it there"
[ -f "$DB" ] || die "no $DB: is the cloud API installed (sudo ./cluster.sh cloud install)?"
[ -w "$DB" ] || die "cannot write $DB: run it with sudo"
command -v python3 >/dev/null || die "python3 is needed"

ADMIN_USER=admin                                        # the provider account cluster.sh created
if [ -f "$CST/cloud.env" ]; then
  a=$(sed -n 's/^VDC_ADMIN_USER=//p' "$CST/cloud.env" | tail -n1)
  [ -n "$a" ] && ADMIN_USER=$a
fi

if [ "$LIST" = 1 ]; then
  python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=10)
rows = c.execute('select u.username, u.role, coalesce(t.name, ""), u.tenant_admin from user u '
                 'left join tenant t on t.id = u.tenant_id order by u.role desc, t.name, u.username').fetchall()
print(f"{'USER':20} {'ROLE':9} {'TENANT':16} ADMIN")
for u, r, t, a in rows:
    print(f"{u:20} {r:9} {t or '-':16} {'yes' if a else ''}")
PY
  exit 0
fi

[ $# -eq 1 ] || usage 1
USER_NAME=$1

if [ "$RANDOM_PW" = 1 ]; then
  PW=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-14)
else
  [ -t 0 ] || die "no terminal to ask for the password: use -r for a random one"
  read -rsp "new password for $USER_NAME: " PW; echo
  read -rsp "again: " PW2; echo
  [ "$PW" = "$PW2" ] || die "the two passwords differ"
fi
[ ${#PW} -ge 8 ] && [ ${#PW} -le 128 ] || die "the password needs 8 to 128 characters (the API's rule)"

# hash and store it; the password goes to python in the environment, not on the command line
VDC_NEWPW=$PW python3 - "$DB" "$USER_NAME" "$(hostname)" <<'PY'
import base64, hashlib, hmac, os, sqlite3, sys
from datetime import datetime, timezone
db, user, host = sys.argv[1:4]
pw = os.environ["VDC_NEWPW"]
salt = os.urandom(16)                                    # the same scheme as cloud/app/auth.py
h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
stored = "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()
c = sqlite3.connect(db, timeout=10)
row = c.execute('select u.id, u.role, coalesce(t.name, "") from user u left join tenant t on t.id = u.tenant_id '
                'where u.username = ?', (user,)).fetchone()
if row is None:
    sys.exit(f"vdc-passwd: no account {user} (sudo ./vdc-passwd.sh -l lists them)")
uid, role, tenant = row
with c:
    c.execute("update user set password_hash = ? where id = ?", (stored, uid))
    c.execute("insert into auditevent (ts, username, role, tenant, action, target, result, detail) "
              "values (?, 'root', '-', ?, 'user.password', ?, 'ok', ?)",
              (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"), tenant, user,
               f"password set with vdc-passwd.sh on {host}"))
# read it back and check it the way the API does at login
_, s, hh = c.execute("select password_hash from user where id = ?", (uid,)).fetchone()[0].split("$")
ok = hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2**14, r=8, p=1), base64.b64decode(hh))
if not ok:
    sys.exit("vdc-passwd: the stored hash does not check out")
print(f"{user} ({role}{', ' + tenant if tenant else ''}): password changed")
PY

if [ "$USER_NAME" = "$ADMIN_USER" ]; then
  (umask 077; printf '%s\n' "$PW" >"$CST/admin-password")
  echo "also written to state/cloud/admin-password (sudo ./cluster.sh cloud admin shows it)"
fi
[ "$RANDOM_PW" = 1 ] && echo "new password: $PW   (shown only now)"
echo "a login $USER_NAME already has stays valid until it expires (8 hours)"

