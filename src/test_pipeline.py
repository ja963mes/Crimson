import pika
import os

creds = pika.PlainCredentials(os.environ['RABBITMQ_USER'], os.environ['RABBITMQ_PASS'])
conn = pika.BlockingConnection(pika.ConnectionParameters('localhost', credentials=creds))
ch = conn.channel()

print("[*] Pulling 3 test domains from incubation...")
test_domains = []

# Safely get 3 messages without clearing them from incubation permanently yet
for _ in range(3):
    method, props, body = ch.basic_get(queue='incubation_queue', auto_ack=False)
    if body:
        domain = body.decode('utf-8')
        test_domains.append((method.delivery_tag, domain))

if not test_domains:
    print("[!] No domains found in incubation_queue to test with.")
else:
    print(f"[*] Found domains: {[d[1] for d in test_domains]}")
    print("[*] Injecting them straight into 'cryptoscams' for the workers...")
    
    for delivery_tag, domain in test_domains:
        # Publish directly to the worker queue
        ch.basic_publish(
            exchange='',
            routing_key='cryptoscams',
            body=domain,
            properties=pika.BasicProperties(delivery_mode=2) # Persistent
        )
        # Acknowledge the original message so it leaves incubation
        ch.basic_ack(delivery_tag=delivery_tag)
        print(f" [->] Injected {domain} into worker queue.")

conn.close()
