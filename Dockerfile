FROM python:3.12-slim-bookworm AS compiler-build
RUN apt-get update && apt-get install -y --no-install-recommends g++ curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY build-compiler.sh /build-compiler.sh
RUN sh /build-compiler.sh /usr/local/bin/thrift

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY thrift_reverse.py thrift_java.py /app/
ENTRYPOINT ["python", "/app/thrift_reverse.py"]
CMD ["/input", "--output", "/output"]

FROM runtime AS validated
ENV THRIFT_JAVA_INPUT=/input THRIFT_JAVA_OUTPUT=/output
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=compiler-build /usr/local/bin/thrift /usr/local/bin/thrift
CMD ["/input", "--output", "/output", "--target", "0.24.0", "--validate"]

FROM validated AS tests
COPY tests /app/tests
ENV THRIFT_BIN=/usr/local/bin/thrift
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "/app/tests", "-v"]
