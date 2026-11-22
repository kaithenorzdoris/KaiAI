FROM rust:1.88 as builder
WORKDIR /app
COPY . .
RUN cargo build --workspace --release

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=builder /app/target/release /app/bin
CMD ["/app/bin/kai-discord"]
