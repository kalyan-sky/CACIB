# Multi-stage build: static Go binary on an empty (scratch) base image.
# No shell, no package manager, runs as a non-root UID - the kind of image
# a bank's container security baseline expects.
#
# The build stage runs natively and cross-compiles, so building on an Apple
# Silicon laptop for amd64 EKS nodes is fast:
#   docker buildx build --platform linux/amd64 ...
FROM --platform=$BUILDPLATFORM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
ARG VERSION=1.4.0
ARG TARGETOS=linux
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /txn-service .

FROM scratch
COPY --from=build /txn-service /txn-service
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/txn-service"]
