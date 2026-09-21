# Architecture

There is one execution spine and one canonical State authority:

```text
Public Engine: imports / algorithms / schemas / policy / assets
       | explicit State root (interface 1)
Private State Git: portfolio + projects + staged publication
       | original generation/CAS, exact command identity and lease
Worker -> Coordinator -> admitted executor -> Report / recovery evidence
       | observations only
127.0.0.1 Owner console
```

The Supervisor performs compact portfolio accounting and one focused review according to `SUPERVISOR_ENTRYPOINT.md`. It stages a request in State rather than editing canonical command/state directly. The original gateway checks tracked freshness, expiration, command bytes/hash, expected generation, based-on-report, Owner control and legal transition. `git_store` performs canonical State CAS. Worker admission protects host capacity, same-project serialization, path conflicts and active-run lease. Recovery reconciles exact evidence rather than replaying uncertain work.

Engine repository updates are independent of State synchronization. Code/assets/schemas are located from Engine; `bridge_root` is the State Git top level. The external Owner gate is not duplicated in either repository. Its location is private non-secret configuration, independently pinned by a local deployment binding.

See `ENGINE_STATE_BOUNDARY.md` for every persistent and ephemeral location. `PROTOCOL.md` and `protocol/v2/` remain protocol authority. Architecture documents explain generic components and contracts; concrete helper/service installation and production rollout facts belong to State and current runtime evidence.
