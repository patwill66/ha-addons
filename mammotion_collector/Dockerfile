ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 sqlite

# collector/ and scripts/ are copied in from ~/Projects/mammotion-luba by tools/build_mammotion_addon.sh.
COPY collector /app/collector
COPY scripts/run_collector.py scripts/inspect_db.py /app/scripts/
COPY SOURCE run.py publish.py /app/
# So scripts/inspect_db.py finds the database when run inside the container.
ENV MAMMOTION_DB_PATH=/share/mammotion_collector/mammotion.db

CMD ["python3", "-u", "/app/run.py"]
