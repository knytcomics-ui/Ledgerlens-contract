# Unsafe Code Policy, Miri and Sanitizer Coverage

The off-chain tool crates parse untrusted input (replay snapshots, RPC
responses, exports), so undefined behaviour or memory errors there are treated
as security bugs. Workflow: `.github/workflows/sanitizers.yml`.

## Policy

- Every tool crate root declares `#![forbid(unsafe_code)]`. Adding `unsafe`
  to a tool crate requires removing the attribute in review, a `// SAFETY:`
  comment on each block, and a new row in the table below.
- Contract crates compile to WASM and contain no `unsafe` in production code.

## Unsafe usage inventory

| Location | Scope | Justification |
|---|---|---|
| `contracts/ledgerlens-score/src/test_gate_fee.rs` (2 × `transmute`) | `#[cfg(test)]` only | Lifetime-extends a generated test client; never compiled into WASM. |
| `contracts/ledgerlens-score/src/test_portfolio_var.rs` (1 × `transmute`) | `#[cfg(test)]` only | Same pattern as above. |
| `tools/*` | none | `#![forbid(unsafe_code)]` enforced at every crate root. |

## Coverage matrix

| Crate | Miri | ASan + LSan | Notes |
|---|---|---|---|
| `replay` | yes (`--lib`) | yes | Pure-Rust parsing and determinism logic. |
| `schema-gen` | yes (`--lib`) | yes | Pure-Rust code generation. |
| `recovery` | no | yes | Binary-only crate driving the Soroban test host; Miri is prohibitively slow and the host uses unsupported intrinsics. |
| `invocation-fuzzer` | no | yes | Runs thousands of contract invocations through the Soroban host; infeasible under Miri's interpreter. |

Rust has no UndefinedBehaviorSanitizer; UB is covered by Miri for the pure
crates and by `-Zub-checks=yes -Cdebug-assertions=yes` (precondition checks in
`core`/`std` unsafe APIs) in the ASan jobs for the rest.

## Triggers

- Nightly at 03:17 UTC and via `workflow_dispatch`.
- On pull requests only when labelled `security`.

## Positive controls (self-test)

A sanitizer job that is misconfigured (wrong target triple, missing nightly
component, flags that never reach the test binary) can pass green while
checking nothing. `sanitizer-self-test` runs in the same workflow as the real
suites and guards against that:

1. It asserts the `miri` and `rust-src` components are installed, failing the job
   loudly instead of letting later steps degrade.
2. It compiles a program with a seeded heap out-of-bounds read and **fails
   unless** ASan reports `heap-buffer-overflow`.
3. It runs the same program under Miri and **fails unless** Miri reports
   `Undefined Behavior`.

Each check also fails if the program dies for any other reason, so a crash that
is not a sanitizer report is not mistaken for detection. Any change to the
sanitizer flags, target or toolchain in `.github/workflows/sanitizers.yml` must
be reviewed against these controls: if a real job gains a new sanitizer, add a
matching seeded-bug control; never make a component install optional.
