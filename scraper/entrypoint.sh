#!/usr/bin/env bash
set -e

echo "RIS Scraper starting"
echo "URL: $RIS_BASE_URL"

while true
do
    python main.py "$RIS_BASE_URL"

    echo "Scrape finished — sleeping ${SCRAPE_INTERVAL}s"
    sleep ${SCRAPE_INTERVAL}
done