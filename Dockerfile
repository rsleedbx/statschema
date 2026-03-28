# statschema test image
#
# Self-contained image with Python 3.11, Java 17, PySpark, and all database
# drivers.  External databases are never started inside the image; they are
# provided as Podman containers running on the host or live cloud endpoints.
#
# Build:
#   docker build -t statschema:latest .
#
# Run offline tests (no DB required):
#   docker run --rm statschema:latest
#
# Run live tests against Podman containers on the host:
#   docker run --rm --network host --env-file .env statschema:latest \
#       tests/test_live_pg.py tests/test_live_mysql.py -v
#
# Run the identity matrix:
#   docker run --rm --network host --env-file .env \
#       --entrypoint python statschema:latest \
#       benchmarks/run_matrix.py identity --engines postgres,mysql --schemas tpch
#
# See docs/test_refactor_plan.md § Phase 8 for full usage documentation.

FROM python:3.11-slim

# Java 17 is required for PySpark local mode
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
        # drivers that need native libs
        libpq-dev \
        # general tooling
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /statschema

# Python dependencies first so they are cached separately from source changes
COPY requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements-test.txt

# Project source: metadata, library, benchmarks, tests
COPY pyproject.toml setup.cfg* setup.py* README.md ./
COPY src/        src/
COPY benchmarks/ benchmarks/
COPY tests/      tests/
COPY config/     config/
COPY conftest.py ./

# Install statschema itself in editable mode
RUN pip install --no-cache-dir -e .

# Default: run offline tests (no database required)
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests/", "-v", "-k", "not live"]
