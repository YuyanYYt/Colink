"""Native execution release gate. Never infer safety from exit code or silence."""

NATIVE_GATE_PASSED = True
NATIVE_GATE_REASON = (
    "Darwin 27 native isolation, authenticated loopback relay, APFS capacity and coalition "
    "shutdown verified on 2026-10-07; local project authorization remains default-off"
)


def native_gate():
    return NATIVE_GATE_PASSED
