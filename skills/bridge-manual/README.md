# Explicit manual helper

Install this skill using the supported local skill installation workflow. The
canonical helper is `<Engine>/worker/bridge_manual.py`. Resolve the Engine from its
installation; never infer State from the current product checkout.

Set `AI_AGENT_BRIDGE_STATE_ROOT` or use the helper's global `--state-root` argument.
`status` reads the State remote through a temporary clone. `start`,
`withdraw-and-start`, and `finish` retain the same Protocol v2 CAS/lease checks.
They do not run Codex, and are never ordinary smoke-test commands.
