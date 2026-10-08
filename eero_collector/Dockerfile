ARG BUILD_FROM
FROM ${BUILD_FROM}

# tzdata: zoneinfo needs it for the network's local day boundaries.
RUN apk add --no-cache python3 sqlite tzdata

# collector/ and scripts/ are copied in from ~/Projects/eero-network by tools/build_eero_addon.sh.
COPY collector /app/collector
COPY scripts/run_collector.py scripts/inspect_db.py /app/scripts/
COPY SOURCE VERSION CHANGELOG.md run.py publish.py mqtt.py web.py /app/
ENV EERO_DB_PATH=/share/eero_collector/eero.db

CMD ["python3", "-u", "/app/run.py"]
