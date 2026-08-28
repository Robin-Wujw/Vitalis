# Morning

Use the requested date, or today's date when none is given.

Render in this order:

1. `decision.action`, recovery state, and confidence.
2. Sleep duration and its 28-day deviation when available.
3. Every current device stream for the selected HRV metric with its 28-day deviation,
   marking the selected stream; include RHR only when present. Never imply that
   deterministic selection means the selected device is more accurate.
4. Suggested training types, intensity, and duration exactly as returned.
5. Drivers followed by material limitations.

When `decision.action` is `INSUFFICIENT_DATA`, render only the data-quality status,
missing required signals, and limitations. Never replace the missing recommendation
with generic training advice.
