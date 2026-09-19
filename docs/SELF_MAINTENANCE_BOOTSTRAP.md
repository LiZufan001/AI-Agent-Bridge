# Split self-maintenance: deliberately closed until independently isolated

Source repository: Public Engine. Canonical maintenance command/report/Goal/Owner records: Private State. Proposed maintenance workdir: an independent Engine-maintenance clone, never running Engine, State, or a linked worktree sharing authority. No extra daemon or repository synchronization protocol is needed.

**The split production entry points reject enabled self-maintenance and both controlled-adoption flags.** This is not a claim that the old adoption implementation was removed or that its tests may be skipped. Those generic tests remain. A full-access executor running as the State-authority user can read/write a sibling State regardless of workdir; a protected-path snapshot detects damage after the fact and is not a preventive security boundary.

The safe currently implemented workflow is offline candidate editing plus CI, independent review and explicit operator deployment while admissions are paused and there are no active runs or unsettled publications/recovery. Running Engine and State are protected paths. A candidate may edit only its independent Engine workdir. State validation must run from an accepted immutable Engine SHA chosen outside the candidate's control; it cannot self-approve by changing its own validator and reference together.

Before unattended self-maintenance can be enabled, demonstrate an executor identity/sandbox that cannot write State or running Engine, verify containment on the actual Windows host, and separately accept the deployment mechanism. This package does not fabricate that proof or silently fall back to full access. Ordinary non-maintenance projects retain their existing execution model and risk assumptions.

Engine rollback with the same compatible State schema must preserve canonical State and local recovery evidence. Rollback to an old combined repository is different: after any canonical mutation, stale combined-repository State is not a lossless rollback target. Never copy State backward or rewind generation automatically.
