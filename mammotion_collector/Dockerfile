ARG BUILD_FROM
FROM ${BUILD_FROM}

# tzdata: zoneinfo needs it for the mower's local days.
RUN apk add --no-cache python3 sqlite tzdata

# collector/ and scripts/ are copied in from ~/Projects/mammotion-luba by tools/build_mammotion_addon.sh.
COPY collector /app/collector
COPY scripts/run_collector.py scripts/inspect_db.py /app/scripts/
COPY SOURCE VERSION CHANGELOG.md run.py publish.py hastats.py web.py /app/
# So scripts/inspect_db.py finds the database when run inside the container.
ENV MAMMOTION_DB_PATH=/share/mammotion_collector/mammotion.db

CMD ["python3", "-u", "/app/run.py"]
