# Contributing to Lunatap

Thanks for your interest in contributing. Bug reports, feature ideas, documentation and code are all welcome.

- **Bugs and ideas**: open an [issue](https://github.com/Parvez2017/lunatap/issues).
- **Pull requests**:
  1. Fork the repository and create a branch (`git checkout -b feat/my-feature`).
  2. Make your changes and run `make test`, `make lint` and `make typecheck`.
  3. Open a pull request with a short summary of the change.

Lunatap is a fork of [google/artemis](https://github.com/google/artemis). Changes that are not iOS-specific (agents, planning, memory) may also be worth proposing upstream.

## Test layers

`make test` is the required, deterministic suite. It does not need an iOS
simulator or device, model credentials, or the optional private cloud service. This is the
same suite used by pull-request CI.

- `make test-integration` runs cross-component tests that may need configured
  model credentials.
- `make test-device` runs end-to-end tests and therefore requires a booted
  iOS simulator or a paired device.
- `make test-all` collects every test tree and is intended for a fully
  provisioned maintainer environment.

Tests that require external state must carry the appropriate `cloud`,
`manual`, or `e2e` marker. They must remain safely collectable when
that dependency is unavailable.
