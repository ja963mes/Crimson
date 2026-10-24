#!/bin/bash
# Installed as /usr/local/bin/crimson-listener-health.sh on the broker (VM4) and run
# every minute by crimson-listener-health.timer.
#
# Restart=always in crimson-listener.service only covers listen.py exiting. On
# Oct 22 it never exited: a ~4 min disk stall on VM4 made RabbitMQ drop it for
# missed heartbeats, listen.py has no RabbitMQ reconnect, and it ran for 28 hours
# logging "Error publishing message to RabbitMQ: Channel is closed" for every
# cert (10.6 GB of log) while publishing nothing. This restarts it when it is
# running but cannot publish. DRY_RUN=1 reports without restarting.

UNIT=crimson-listener.service
LOG=/home/ubuntu/crimson/src/sender-logs/websocket_listener.log
GRACE=90         # seconds after a start before checking; the first connect takes ~10 s
LOG_WINDOW=120   # a publish error newer than this means publishing is failing now

say() { echo "$*"; }
restart() {
    if [ "${DRY_RUN:-0}" = 1 ]; then say "unhealthy ($1), DRY_RUN: not restarting"; exit 0; fi
    say "unhealthy ($1), restarting $UNIT"
    systemctl restart "$UNIT"
    exit 0
}

# Nothing to revive while systemd itself is (re)starting it or the broker is down:
# the unit Requires rabbitmq-server and is restarted along with it.
systemctl is-active --quiet rabbitmq-server.service || { say "rabbitmq-server not active, skipping"; exit 0; }
systemctl is-active --quiet "$UNIT" || { say "$UNIT not active, left to systemd"; exit 0; }

pid=$(systemctl show -p MainPID --value "$UNIT")
started=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")
now=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
if [ $(( (now - started) / 1000000 )) -lt $GRACE ]; then say "started under ${GRACE}s ago, skipping"; exit 0; fi

# 1. The AMQP connection. A heartbeat timeout or broker-side close drops the TCP
#    connection, and listen.py never opens another.
if ! ss -tnpH state established '( dport = :5672 )' | grep -q "pid=$pid,"; then
    restart "pid $pid has no connection to RabbitMQ"
fi

# 2. A connection that is up but whose channel is closed. listen.py logs only
#    errors and feed reconnects, so a fresh publish error as the last line means
#    publishes are failing now.
if [ -f "$LOG" ] && [ $(( $(date +%s) - $(stat -c %Y "$LOG") )) -lt $LOG_WINDOW ] \
   && tail -n 1 "$LOG" | grep -q 'Error publishing message to RabbitMQ'; then
    restart "publish errors in $(basename "$LOG")"
fi

say "healthy (pid $pid)"
