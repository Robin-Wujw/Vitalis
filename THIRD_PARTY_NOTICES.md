# Third-party notices

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
