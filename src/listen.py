import os
import time
import threading
import websocket
import pika
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import timezone, timedelta

"""
listen.py is a messenger. It listens to a CertStream WebSocket feed and sets up the
RabbitMQ pipeline, then forwards the raw data to the RabbitMQ message broker.

The feed is now an EXTERNAL CertStream server rather than the self-hosted one that
cron.py used to supervise. Override with the CERTSTREAM_URL env var; cron.py and
certstream-server/ are left in place so reverting to the local feed only means
setting CERTSTREAM_URL=ws://130.245.32.96:4000 and starting cron.py again.
"""

# Configuration
# External CertStream feed. Emits 'certificate_update' frames with
# data.leaf_cert.all_domains (what send.py parses) plus periodic 'heartbeat'
# frames, which send.py discards via its message_type guard.
CERTSTREAM_URL = os.environ.get('CERTSTREAM_URL', 'ws://130.245.32.192:8080/')
RABBITMQ_HOST = 'localhost'
QUEUE_NAME = 'urls'
LOG_DIR = "sender-logs"
REPORT_LOG_DIR = "hourly_reports"

# Reconnect/liveness tuning. These matter now that the feed is remote: a local
# socket never really dropped, but a network one will.
PING_INTERVAL = 30   # seconds; detects half-open TCP that NAT/firewalls leave "established"
PING_TIMEOUT = 10    # seconds to wait for pong before declaring the link dead
INITIAL_BACKOFF = 1  # seconds
MAX_BACKOFF = 60     # cap, so a feed outage doesn't hammer someone else's server

# Setup logging
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(REPORT_LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "websocket_listener.log")
REPORT_LOG_FILE = os.path.join(REPORT_LOG_DIR, "hourly_report.log")

logger = logging.getLogger("WebSocketListenerLogger")
logger.setLevel(logging.DEBUG)
handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
handler.suffix = "%Y-%m-%d"
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger.addHandler(handler)

report_logger = logging.getLogger("HourlyReportLogger")
report_logger.setLevel(logging.INFO)
report_handler = TimedRotatingFileHandler(REPORT_LOG_FILE, when="midnight", interval=1, backupCount=7)
report_handler.suffix = "%Y-%m-%d"
report_handler.setFormatter(formatter)
report_logger.addHandler(report_handler)

class CryptoScamListener:
    def __init__(self):
        self.connection = None
        self.channel = None
        self.message_count = 0
        self.hourly_message_count = 0
        self.ws = None
        self.backoff = INITIAL_BACKOFF
        self.setup_rabbitmq_connection()
        self.schedule_hourly_report()

    def setup_rabbitmq_connection(self):
        while True:
            try:
                self.connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
                self.channel = self.connection.channel()
                self.channel.queue_declare(queue=QUEUE_NAME, durable=True)
                logger.info("RabbitMQ Connection established.")
                break
            except Exception as e:
                logger.exception("Error connecting to RabbitMQ:")
                time.sleep(1)

    def report_hourly_messages(self):
        report_logger.info(f"Messages in last hour: {self.hourly_message_count}, Total: {self.message_count}")
        self.hourly_message_count = 0
        self.schedule_hourly_report()

    def schedule_hourly_report(self):
        threading.Timer(3600, self.report_hourly_messages).start()

    def on_message(self, ws, message):
        self.message_count += 1
        self.hourly_message_count += 1
        try:
            # FIX: Pass 'message' directly to avoid double-encoding the JSON string
            self.channel.basic_publish(
                exchange='', 
                routing_key=QUEUE_NAME, 
                body=message, 
                properties=pika.BasicProperties(delivery_mode=2)
            )
        except Exception as e:
            logger.error(f"Error publishing message to RabbitMQ: {e}")

    def on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        # Reconnection is driven by the supervising loop in run(), NOT from here.
        # Reconnecting inside this callback used to spawn a fresh listener thread on
        # every drop, with no backoff: against localhost that never fired, but against
        # a remote feed it would spin and could leave two listener threads publishing
        # concurrently -- and pika's BlockingConnection is not thread-safe.
        logger.warning(f"WebSocket closed: {close_status_code} {close_msg}")

    def on_open(self, ws):
        logger.info(f"WebSocket connection opened: {CERTSTREAM_URL}")
        self.backoff = INITIAL_BACKOFF  # connection is good again; reset the ramp

    def run(self):
        """
        Supervise a single listener in THIS thread, reconnecting with capped
        exponential backoff. run_forever() returns whenever the link drops, so the
        loop re-establishes it; only one socket is ever live, which keeps the pika
        publish in on_message single-threaded.
        """
        try:
            while True:
                self.ws = websocket.WebSocketApp(
                    CERTSTREAM_URL,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                self.ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT)
                logger.warning(f"Feed disconnected. Reconnecting in {self.backoff}s...")
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if self.ws:
                self.ws.close()
            if self.connection:
                self.connection.close()

if __name__ == "__main__":
    listener = CryptoScamListener()
    listener.run()
