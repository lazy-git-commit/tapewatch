# Contributing to Tapewatch

Thanks for your interest. This project has unusually strict testing conventions
for a hobby-scale codebase, and there is a reason for each of them — this
document explains both the mechanics and the reasoning.

---

## Before you start

**Security issues:** do not open a public issue. See [`SECURITY.md`](SECURITY.md).

**Large changes:** open an issue first to discuss the approach. A rejected pull
request that took a weekend is a bad outcome for everyone.

**The contributor licence agreement:** your first pull request will get an
automated comment asking you to sign. Reply on the PR with exactly:

```
I have read the CLA Document and I hereby sign the CLA
```

You keep copyright in your work — see [`CLA.md`](CLA.md) for why this exists.
It is a one-time step.

---

## Development setup

```bash
git clone https://github.com/lazy-git-commit/tapewatch.git
cd tapewatch

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

pytest tests/ -q                   # should pass with no credentials at all
```

**The test suite needs no API keys, no database and no network.** Everything
external is mocked. If a test you write requires credentials, that test is
testing the wrong thing.

For running the system itself, see
[`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md).

---

## The testing conventions

### 1. Tests must fail when the feature is removed

This is the rule that matters most. A test named after a behaviour that still
passes when you delete the behaviour is worse than no test — it is a false
guarantee.

Before submitting, **break your own feature deliberately and confirm a test
catches it.** In this repository that is called mutation testing, and every
significant change has been through it. The changelog records the count for each
version, along with the mutations that initially *survived* and what that
revealed.

Two real examples from this project's history:

- A test class named `TestOutputBudgetAndTruncation` passed cleanly with the bug
  it was written for reintroduced. It asserted against the wrong client and used
  a response shape that took a different code path.
- A deduplication test passed with the ranking logic disabled, because the
  fixture data happened to order confidence and record ID the same way. It was
  fixed with a case where the two disagree.

### 2. Run mutation checks on a copy, not your working tree

An in-place mutate-and-restore script once corrupted a source file and left
stale bytecode behind, producing three test failures against source that was
actually correct. Copy the repository to a temporary directory, mutate there,
and clear `__pycache__` between runs.

### 3. Comments explain *why*, not *what*

The code says what it does. A comment earns its place by recording the reasoning
or the incident behind a decision — especially a decision that looks wrong at a
glance. Most non-obvious constants in this codebase have a story attached, and
that story is why nobody has "simplified" them back into a bug.

### 4. Never widen a gate without evidence

Trading gates exist because something went wrong. If you want to relax one,
bring a measurement — and make it **path-aware**, using
`analysis/triple_barrier.py`, not a fixed-horizon forward return. Those two
methods have disagreed and given opposite conclusions on this exact codebase.

### 5. No performance claims

Do not add profitability claims, returns figures presented as an inducement, or
equity curves to the README or documentation. Measured results, including
negative ones, belong in [`docs/RESULTS.md`](docs/RESULTS.md) with their method
stated. This is both an honesty rule and a legal one.

---

## Pull request checklist

- [ ] `pytest tests/ -q` passes
- [ ] New behaviour has a test that fails when the behaviour is removed
- [ ] Comments explain the reasoning behind anything non-obvious
- [ ] Documentation updated if behaviour or configuration changed
- [ ] `CHANGELOG.md` updated for anything user-visible
- [ ] No credentials, hostnames, IP addresses or personal data added
- [ ] New source files carry the Apache licence header (see below)
- [ ] CLA signed

---

## Licence header

Every source file carries this header. New files should too — copy it
verbatim, and **do not add your own copyright line**. Ownership is recorded
once in [`NOTICE`](NOTICE) rather than repeated in every file, which is the
Apache Software Foundation's own practice and avoids per-file ownership
notices going stale as a file gains contributors. You keep the copyright in
your work either way — see [`CLA.md`](CLA.md).

```python
# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
```

---

## Adding a provider

Adding support for a new news source, quote provider or broker does not require
changing any trading logic. See [`docs/PROVIDERS.md`](docs/PROVIDERS.md) for the
contract each must satisfy, and a worked example.

Provider contributions are especially welcome — the project currently supports
one broker and two quote sources, and broadening that helps everyone.

---

## Code style

- Follow the surrounding code. It is consistent; match it rather than importing
  your own conventions.
- Type hints on function signatures.
- Module-level docstrings explaining the module's job and its failure modes.
- Log at `INFO` for anything an operator would want to see in production, and
  `DEBUG` for detail. A real outage that only logs at `DEBUG` is invisible —
  this has happened here and cost a day of diagnosis.

---

## What gets a change rejected

- Widening a risk gate without path-aware evidence
- Removing a test to make a build pass
- Adding a dependency for something the standard library does adequately
- Performance claims anywhere in the documentation
- Anything that makes the system fail *open* on missing data
