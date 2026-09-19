# Dependency and capability boundary

## Source runtime

The inspected Python imports resolve to the Python standard library or modules
in this repository. Python 3.11+ is the documented interface floor; acceptance
of an individual release is limited to the versions recorded in that release's
test evidence. This statement is not a claim of tested support for every Python
release. Git is an external executable required for Git-backed operations.

The web console ships local HTML/CSS/JavaScript assets; it does not require a
CDN dependency. State schemas, templates and the synthetic State fixture are
part of the source tree. Real State and deployment configuration are external
instance inputs, never packaging dependencies to be copied from an owner.

## Optional platform and execution capabilities

Windows-specific process, ACL, scheduling and network code uses Windows APIs and
system utilities. Configured Codex execution requires a separately installed
executor. These programs, the operating system and their credentials are not
bundled or relicensed by this repository.

The privileged-helper protocol has an optional separately installed native
implementation (`AI-Agent-Bridge.Phase86.Helper.exe`). This source distribution
does not ship that binary or its native build sources. Missing accepted helper
installation is a capability prerequisite failure, not a reason to silently
substitute the in-memory reference backend or relax containment. Enabling that
capability needs a separate installation, supply-chain and native acceptance
record. Retained Phase86 names are compatibility identifiers, not an owner's
machine identity or a statement that native enforcement was verified by CI.

## CI

CI uses pinned upstream actions/checkout and actions/setup-python commits with
read-only repository permissions and no persisted checkout credentials. Hosted
runner images and the selected Python patch version remain external moving
inputs; record their actual versions in any release evidence. CI is not an
OS-level network sandbox and must never receive production credentials or State.
