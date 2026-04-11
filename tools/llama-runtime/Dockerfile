# syntax=docker/dockerfile:1.6

ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.4.1-devel-ubuntu22.04
FROM ${CUDA_DEVEL_IMAGE} AS build

ARG LLAMA_CPP_REPO=https://github.com/ggml-org/llama.cpp.git
ARG LLAMA_CPP_REF=master

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone "${LLAMA_CPP_REPO}" llama.cpp

WORKDIR /src/llama.cpp
RUN git fetch --all --tags \
    && git checkout "${LLAMA_CPP_REF}"

RUN cmake -S . -B build \
      -DGGML_CUDA=ON \
      -DLLAMA_BUILD_SERVER=ON \
      -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --config Release --target llama-server -j"$(nproc)"

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server

EXPOSE 2020

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=20 \
  CMD curl -fsS http://127.0.0.1:2020/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/llama-server"]
