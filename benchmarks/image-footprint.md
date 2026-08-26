# Production image footprint

Measured on 2026-08-25 from the pinned Hermes v2026.8.3 base on Linux arm64.
Docker's image size is the uncompressed local content size; registry transfer
size varies by platform and compression.

## Baseline

| Image | Size | Added over Hermes |
| --- | ---: | ---: |
| Pinned Hermes base | 2,754,370,653 bytes | — |
| Review Agent before cleanup | 2,817,548,619 bytes | 63,177,966 bytes |
| Review Agent after cleanup | 2,780,156,196 bytes | 25,785,543 bytes |

The single dependency layer added 62 MB. The base already contained `curl` and
CA certificates. The Review Agent layer installed them again and added the
34,650 KiB GitHub CLI package, although the runtime disables terminal access and
all GitHub reads and writes use typed clients.

## Accepted change

- Remove the redundant apt transaction and GitHub CLI package.
- Keep `psycopg[binary]`: its self-contained native implementation is useful on
  both release platforms and avoids trading runtime efficiency for image size.
- Send only `requirements.txt`, `bootstrap/`, and the Review Agent entrypoint
  modules as Docker build context.

The final image is 37,392,423 bytes smaller: a 59.2% reduction in the Review
Agent-owned layers. Its dependency layer fell from 62 MB to 24.6 MB, and the
bytecode-free bootstrap layer remained 924 kB.

The production build retained the pinned Hermes identity, arbitrary-UID
installer, admission, worker, publisher, contract, and Hermes gateway smokes,
health-check `curl`, psycopg 3.3.4, psycopg-pool 3.3.1, and PyYAML 6.0.3. The
final image ID was
`sha256:35e7b749398131e52560edc2c7c9301a0dffe15d3b36013a17bb4582876f208a`.
