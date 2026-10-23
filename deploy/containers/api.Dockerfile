FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/anvil-api ./cmd/anvil-api

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/anvil-api /anvil-api
EXPOSE 8080
ENTRYPOINT ["/anvil-api"]
