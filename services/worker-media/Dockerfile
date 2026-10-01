FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
# go.sum dibuat otomatis saat build (butuh internet)
RUN go mod tidy && CGO_ENABLED=0 go build -o /worker .

FROM alpine:3.20
RUN apk add --no-cache ffmpeg ca-certificates && adduser -D app
COPY --from=build /worker /worker
USER app
CMD ["/worker"]
