FROM node:22-alpine AS zimui

WORKDIR /src
COPY zimui /src
RUN yarn install --frozen-lockfile
RUN yarn build

FROM python:3.12-bookworm
LABEL org.opencontainers.image.source=https://github.com/openzim/mindtouch

# Install necessary packages
RUN apt-get update \
     && apt-get install -y --no-install-recommends \
     wget \
     unzip \
     ffmpeg \
     aria2 \
     && rm -rf /var/lib/apt/lists/* \
     && python -m pip install --no-cache-dir -U \
     pip

RUN mkdir -p /output
WORKDIR /output

# Copy pyproject.toml and its dependencies
COPY README.md /src/
COPY scraper/pyproject.toml scraper/openzim.toml /src/scraper/
COPY scraper/src/mindtouch2zim/__about__.py /src/scraper/src/mindtouch2zim/__about__.py

# Install Python dependencies
RUN pip install --no-cache-dir /src/scraper

# Copy code + associated artifacts
COPY scraper/src /src/scraper/src
COPY *.md LICENSE /src/

# Install + cleanup
RUN pip install --no-cache-dir /src/scraper \
 && rm -rf /src/scraper

# Copy zimui build output
COPY --from=zimui /src/dist /src/zimui

ENV MINDTOUCH_ZIMUI_DIST=/src/zimui \
    MINDTOUCH_OUTPUT=/output \
    MINDTOUCH_TMP=/tmp\
    MINDTOUCH_CONTACT_INFO=https://www.kiwix.org

CMD ["mindtouch2zim", "--help"]
