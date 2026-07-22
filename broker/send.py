import os
import pika
import json
import logging
import pytz
from datetime import datetime
from collections import OrderedDict
from logging.handlers import TimedRotatingFileHandler
import time
import utils.keyword_utils as kw_utils

"""
send.py is a filter. it takes the urls queue which has every cert issued and then filters it for the ones that are likely to be scams.
it then forwards the urls that pass the filter to the incubation waiting room.
"""

# Configuration
RABBITMQ_HOST = 'localhost'
QUEUE_NAME = 'cryptoscams'
LOG_DIR = "sender-logs"
CACHE_CAPACITY = 50000

# Setup logging
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "send.log")
logger = logging.getLogger("SendLogger")
logger.setLevel(logging.DEBUG)
handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7)
handler.suffix = "%Y-%m-%d"
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger.addHandler(handler)

# Timezone configuration
newYorkTz = pytz.timezone("America/New_York")

# Dedupe cache. CACHE_CAPACITY was previously declared but never used, so the
# same domain was republished on every cert reissue, and every wildcard-cert
# subdomain flooded through independently (e.g. dozens of *.eth.flights hosts).
# recv.py catches these as 'Dup', but only AFTER they traverse the 12h
# incubation queue and consume a worker dequeue. Dedupe belongs here instead.
seen_domains = OrderedDict()

def dedupe_key(url_name):
    """
    Normalized cache key. 'www.foo.com' and 'foo.com' are the same site, but were
    being published (and independently crawled) as two separate domains.
    We normalize only the CACHE KEY -- the original domain string is still what
    gets published, because some hosts only serve on the www. name and stripping
    it would turn them into false 'unreachable' results in recv.py.
    """
    return url_name[4:] if url_name.startswith('www.') else url_name

def already_seen(url_name):
    """LRU membership test. Returns True if url_name was recently published."""
    key = dedupe_key(url_name)
    if key in seen_domains:
        seen_domains.move_to_end(key)
        return True
    seen_domains[key] = True
    if len(seen_domains) > CACHE_CAPACITY:
        seen_domains.popitem(last=False)
    return False

def parse_domain_name(domain_name):
    return domain_name[2:] if domain_name.startswith('*') else domain_name

def log_domains(url_name, curr_date, filename, mode='a'):
    log_path = os.path.join("logs", curr_date)
    os.makedirs(log_path, exist_ok=True)
    with open(os.path.join(log_path, filename), mode) as f:
        f.write(f'{url_name}\n')

def enqueue_domains(message, context, channel):
    if message['message_type'] != "certificate_update":
        return
    all_domains = message['data']['leaf_cert']['all_domains']
    curr_date = str(datetime.now(newYorkTz)).split(' ')[0].replace('-', '')[2:]
    for each_domain in all_domains:
        url_name = parse_domain_name(each_domain.lower())
#       log_domains(url_name, curr_date, 'all_domains_seen.txt', 'a')
        if not kw_utils.match_domain_name_with_keywords(url_name):
#           log_domains(url_name, curr_date, 'failed_url_filter.txt', 'a')
            continue
        log_domains(url_name, curr_date, 'passed_url_filter.txt', 'a')

        # Skip domains already published recently (cert reissues, wildcard subdomains)
        if already_seen(url_name):
            log_domains(url_name, curr_date, 'deduped.txt', 'a')
            continue

        # Route to the 12-hour incubation waiting room
        channel.basic_publish(
            exchange='',
            routing_key='incubation_queue',
            body=url_name,
            properties=pika.BasicProperties(delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE)
        )
        log_domains(f" [x] Sent {url_name}", curr_date, 'sent.txt', 'a')

if __name__ == '__main__':
    # 1. Setup the authenticated connection
    credentials = pika.PlainCredentials(os.environ['RABBITMQ_USER'], os.environ['RABBITMQ_PASS'])
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=RABBITMQ_HOST, credentials=credentials)
    )
    channel = connection.channel()

    # 2. Ensure listening and sending queues exist
    channel.queue_declare(queue='urls', durable=True)
    channel.queue_declare(queue=QUEUE_NAME, durable=True)

    # 3. Create the 12-hour incubation waiting room (43200000 ms)
    incubation_args = {
        'x-dead-letter-exchange': '',
        'x-dead-letter-routing-key': QUEUE_NAME,  # Where it goes when it dies
        'x-message-ttl': 43200000
    }
    channel.queue_declare(queue='incubation_queue', durable=True, arguments=incubation_args)

    def callback(ch, method, properties, body):
        try:
            message = json.loads(body)
            if isinstance(message, str):
                message = json.loads(message)
            enqueue_domains(message, None, ch)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_qos(prefetch_count=100)
    channel.basic_consume(queue='urls', on_message_callback=callback, auto_ack=False)

    print(' [*] Filter active. Waiting for domains in urls queue. To exit press CTRL+C')
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        print("\nShutting down filter...")
        connection.close()
