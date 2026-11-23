


#!/bin/bash

# Usage check
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <directory>"
    exit 1
fi

search_dir=$1  # Directory to search in, provided as an argument

# Define the text file to write the results
results_file="$search_dir/iocs_results"
processed_files_log="$search_dir/iocs_results.log"

# Create the results file if it doesn't exist. It is appended to, not cleared:
# files already in the processed log are skipped below, so clearing it on every
# run (as this used to) lost the IOCs found by earlier runs. crimson-crawler.service
# runs this after every crawl pass.
touch "$results_file"

# Ensure processed files log exists
touch "$processed_files_log"

# Specify the IOC types to search for. Current iocsearcher calls Tron addresses
# (TRX and USDT-TRC20) "tron"; with "tronix" it printed "No regexp for target"
# and skipped them.
#
# Only the address formats of the coins we report (Nov 22 2026): BTC, BCH, ADA,
# DASH, DOGE, LTC, XRP, TRX + USDT-TRC20 ("tron"), and ETH + BNB (BSC) + MATIC +
# USDT/USDC on EVM chains, which all share the 0x format ("ethereum";
# build_wallets.py tells them apart by the page label). Monero, tezos, zcash,
# webmoney, onion addresses, e-mail, phone numbers and social handles are no
# longer searched: build_wallets.py reads wallet types only, and the e-mail regex
# alone ran over 90 s on an 11 MB page on VM1, stalling the whole pass nightly
# (Nov 20-22).
ioc_types=(bitcoin bitcoincash cardano dashcoin dogecoin ethereum litecoin ripple tron)

# Per-file time limit. A file that hits it is recorded as processed (and in
# iocs_timeouts.log) so it cannot stall every later pass; before this one page
# was retried every night until systemd's 1 h stop-post limit killed the pass.
IOC_FILE_TIMEOUT=${IOC_FILE_TIMEOUT:-120}
timeouts_log="$search_dir/iocs_timeouts.log"

 target_options=""
for ioc_type in "${ioc_types[@]}"; do
    target_options+="-t $ioc_type "
done

 if [ ! -d "$search_dir" ]; then
    echo "The specified path is not a directory: $search_dir" | tee -a "$results_file"
    exit 2
fi

# Iterate over HTML files in the directory and subdirectories and perform searches
find "$search_dir" -type f -name '*.html' | while read html_file; do
    # Check if the file has been processed already
    if grep -Fxq "$html_file" "$processed_files_log"; then
        echo "Skipping already processed file: $html_file"
    else
        echo "Searching IOCs in $html_file" | tee -a "$results_file"
        # Execute iocsearcher on each HTML file and append output to the results file
        timeout -k 10 "$IOC_FILE_TIMEOUT" iocsearcher -f "$html_file" -F $target_options 2>&1 | tee -a "$results_file"
        status=${PIPESTATUS[0]}
        if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
            echo "TIMED OUT after ${IOC_FILE_TIMEOUT}s: $html_file" | tee -a "$results_file"
            echo "$(date -u +%FT%TZ) $html_file" >> "$timeouts_log"
        fi
        echo "---------------------------------------" | tee -a "$results_file"
        # Log that this file has been processed
        echo "$html_file" >> "$processed_files_log"
    fi
done

echo "All IOC searches completed." | tee -a "$results_file"