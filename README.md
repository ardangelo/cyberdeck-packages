# Cyberdeck package sources

Service code and its Debian recipe live in
[ardangelo/cyberdeck-service](https://github.com/ardangelo/cyberdeck-service),
on `master`. Its `mobileapp` and `src/nanopb` entries remain real Git submodules.
This repository owns build orchestration; do not copy service code or maintain
another service `debian/` here.

## Clean source staging

`scripts/sources.py` provides the source-staging operations:

- `fetch(repository, revision, destination, recursive=True)` fetches an exact
  40-character commit over anonymous GitHub HTTPS. It initializes the declared
  gitlinks and rejects dirty checkouts or revision mismatches. Its returned
  submodule provenance comes from those gitlinks, not a parallel pin list.
  Readiness-only fetches use `recursive=False`: they verify the SDK commit and
  clean parent checkout without fetching unrelated source submodules.
- `export(checkout, destination, debian=True)` exports tracked files through
  separate Git archives for the parent and each submodule. It excludes local
  configuration, caches, generated native/protocol objects, build directories
  and distribution JARs. The tracked Gradle wrapper JAR remains included.
  Each exported repository receives `source-revision.txt` containing its exact
  commit. Git-free Gradle builds use the mobile submodule's own marker;
  maintained repositories keep their real gitlinks and contain no generated
  marker. A conflicting marker or changed gitlink fails export.
- `archive(source, output, prefix, epoch)` writes a deterministic gzip-compressed
  source archive with normalized owners, timestamps and permissions, preserving
  executable bits. Set `debian=False` when
  exporting the upstream component of a Debian `3.0 (quilt)` source package;
  stage the canonical Debian metadata separately.

Source staging requires Git and Python 3.12 or newer. Export into a fresh
throwaway directory. Never use an existing development worktree or its compiled
outputs as a package input.

The regression suite exercises archive-byte stability across host permission
differences and verifies that source-content changes alter the archive:

```sh
python3 -m unittest discover -s tests -v
```

## Service build environment

`containers/service.Dockerfile` accepts these build arguments without defaults:

- `BASE_IMAGE`: the locked `linux/amd64` Debian image digest.
- `DEBIAN_SNAPSHOT`: `20250809T000000Z`.
- `JDK17_URL` and `JDK17_SHA256`: the exact Temurin 17 GA HotSpot Linux/x64 archive.
- `ANDROID_COMMANDLINE_URL` and `ANDROID_COMMANDLINE_SHA256`: the exact official
  Android command-line tooling archive.
- `ANDROID_PLATFORM_URL` and `ANDROID_PLATFORM_SHA256`: the platform 36 archive.

Every tooling download is SHA-256 checked before extraction. The build JVM is
Debian OpenJDK 21; the Gradle Java toolchain is `/opt/jdk-17`. Gradle installation
paths are explicit and automatic toolchain downloads are disabled. Android
inputs live under `/opt/android-sdk`.

The container first installs CA certificates from the signed frozen Debian
snapshot over HTTP, then uses HTTPS. Signature checks remain enabled throughout.
Only that historical Debian source disables `Valid-Until`. Snapshot priority
1001 permits matching native and RISC-V dependency versions even when the base
image contains newer security updates; downgrades are confined to this disposable
build environment. Do not carry that policy into device APT configuration.

Bind-mount disposable source and artifact directories at build time. The image
contains tooling and dependencies, not maintained service code, a fat JAR or
package outputs. Signing credentials do not belong in this container.

## Debian source ownership

- `packages/status-ready/debian/` packages the SDK's `k230-status-ready`
  helper as `Architecture: all`. Include the SDK's root `LICENSE` with its
  upstream source. Debhelper installs and manages the systemd unit; the package
  does not ship a pre-created enablement symlink. Set
  `K230_STATUS_READY_SYSFS` to a writable temporary file when exercising the
  helper without hardware.
- `packages/sharp-drm/debian/` defines `k230-sharp-drm` and `k230-kernel-abi`,
  both for `riscv64`. The module requires the marker at exactly its own version.
  Kbuild supplies the module bytes; stripping is disabled. The modules-load
  entry and `mono_cutoff=64` parameter remain package-owned.
- The service's canonical `debian/` defines the fourth binary,
  `cyberdeck-service`, for `riscv64`. Build from that recipe, including its
  dependency-license verification. Retain the generated dependency-source
  reports as corresponding-source artifacts; a clean Git export alone does
  not include the downloaded dependency sources.

All three source packages use `3.0 (quilt)`. Their Debian metadata and upstream
source archives are separate inputs to `dpkg-source`; keep both components
with the generated `.dsc`.
