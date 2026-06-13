# The Poorest Man in Babylon: A Longitudinal Study of Cryptocurrency Investment Scams
This repository includes the artifacts from the ACM Web Conference 2025 (WWW'25) paper entitled "The Poorest Man in Babylon: A Longitudinal Study of Cryptocurrency Investment Scams" by Muhammad Muzammil, Abisheka Pitumpe, Xigao Li, Amir Rahmati, and Nick Nikiforakis [(PDF)](https://muhammad-muzammil.github.io/www-crimson-25.pdf).

- `data/` contains a JSON file of our dataset of cryptocurrency investment scam websites detected by Crimson.
- `src/` contains the source code for each module in Crimson
  - `cron.py`: This script manages the execution of the [(certstream server)](https://github.com/CaliDog/certstream-server) by continuously monitoring its status and restarting it if necessary.
  - `listen.py`: This script listens to a WebSocket server, processes incoming messages, and publishes them to a RabbitMQ queue.
  - `send.py`: This script listens for certificate update messages, filters domain names using a keyword-matching utility, and publishes the filtered domain names to a RabbitMQ queue. It maintains logging for domains that pass or fail the filtering process.
  - `recv.py`: This script processes URLs from a RabbitMQ queue and checks for potential cryptocurrency scam websites using Object Character Recognition (OCR).
  - `validate.py`: This script processes OCR-extracted text from a CSV file, interacts with a local LLM model via subprocess calls, and classifies text as scam-related or not.
  - `authentication_crawling/crawler_script.py`: This script is a web automation crawler that automates sign-up and login processes on various websites, and then further crawls the websites to look for cryptocurrency addresses and other IOCs.
  
If you use this work, please use this citation:
```
@inproceedings{muzammil2025crimson,
  title = {{The Poorest Man in Babylon: A Longitudinal Study of Cryptocurrency Investment Scams}},
  author = {Muhammad Muzammil and Abisheka Pitumpe and Xigao Li and Amir Rahmati and Nick Nikiforakis},
  booktitle = {Proceedings of the Web Conference (WWW)},
  year = {2025},
}
```

## Repository layout (lab deployment)

| Folder | Runs on | Contents |
|---|---|---|
| `broker/` | the RabbitMQ hub (VM4) | `listen.py` (CertStream listener), `send.py` (filters), `cron.py`, `test_pipeline.py`, `utils/keyword_utils.py`, the health-check script and `systemd/` units |
| `worker/` | the worker VMs (VM1-3) | `recv.py` (LLM worker), `validate.py`, `utils/` and `systemd/` units |
| `worker/crawler/` | the worker VMs (VM1-3) | the authentication crawler: `crawler_script.py`, `feeder.py`, `build_wallets.py`, `hyip_classifier.py`, `submit_scambusters.py`, `iocs_searcher.sh` |
| `tools/` | a local PC | evaluation and test tools: regression runner, Browser Use agent, classifier checks, HYIP feature research |

On the VMs the scripts live in `~/crimson/src/` (crawler in `~/crimson/src/authentication_crawling/`). Units are in `/etc/systemd/system/`.

The history was rebuilt on 2026-11-22 from the backups kept before each change, so each commit carries the date that version was written. Machine addresses and secrets are replaced with placeholders such as `<BROKER_IP>`, `<VM1_IP>`, `<MAILINATOR_API_KEY>` and `<MAILINATOR_DOMAIN>`. Put the real values back, or read them from the environment, before you deploy.
