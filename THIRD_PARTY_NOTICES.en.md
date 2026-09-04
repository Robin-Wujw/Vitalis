# Third-party notices

[中文](THIRD_PARTY_NOTICES.md)

> This English notice is authoritative. The Chinese translation is provided for accessibility. The MIT license text below is authoritative and is reproduced verbatim in both language files.

## ZeppBridge integration provenance

Some Zepp cloud endpoint, payload-normalization, and region-probing conventions in
`vitalis/connectors/zepp/` were developed with reference to ZeppBridge.

- Upstream repository: <https://github.com/lingcang728/ZeppBridge>
- Reviewed release: `v2.1.0` (2026-09-02); the precise historical source revision for
  existing translated conventions has not yet been reconstructed.
- Vitalis modules with explicit ZeppBridge references: `auth_parser.py`, `client.py`,
  `fetcher.py`, `parser.py`, `sync_manager.py`, and `api/routes/connect.py`.
- Vitalis-specific changes: Python/FastAPI/SQLAlchemy architecture, typed normalized
  contracts, source/scope/device provenance, bounded chunk orchestration, leases,
  retries, diagnostics, and deterministic analysis boundaries.
- Upstream license: MIT. ZeppBridge also identifies Apache-2.0-derived workout
  decoding in its upstream notice file. Before copying or adapting additional upstream
  code, record the exact revision and preserve all applicable MIT and Apache notices.

This entry records provenance and does not claim that Vitalis is affiliated with,
endorsed by, or functionally equivalent to ZeppBridge or Zepp Health.

## OpenStrap analytics

Portions of the Open Health Insights shadow algorithms are ported or adapted from OpenStrap analytics.

- Upstream repository: <https://github.com/OpenStrap/analytics>
- Upstream revision: `45d72ed989c004008b919b366cd5ceda7061b7df`
- Vitalis modules: `vitalis/intelligence/open_health/ewma.py`, `readiness.py`, `anomaly.py`, `sleep.py`, and `load.py`.
- Upstream-derived scope: winsorized EWMA conventions, nightly lnRMSSD readiness structure, robust median/MAD anomaly conventions, Banister TRIMP, and ATL/CTL/TSB exponential-load conventions.
- Vitalis-specific changes: typed `OpenHealthInsights 1.0` envelopes, source/device isolation, user-confirmed profile gates, coverage/refusal policy, hard-reject and stale handling, SWC readiness bands, 99.9% anomaly threshold and persistence policy, sleep timing/regularity policy, workout pause/gap handling, upstream-coverage lower bounds, non-diagnostic wording, and `shadow_only` isolation from Decision Policy 7.0.

Vitalis does not claim equivalence to WHOOP, OpenStrap hardware, or any proprietary recovery/readiness score. These algorithms are descriptive personal-statistical helpers and are not medical diagnosis or treatment advice.

### MIT License

Copyright (c) 2026 OpenStrap

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
