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
- `export(checkout, destination, debian=True)` exports tracked files through
  separate Git archives for the parent and each submodule. It excludes local
  configuration, caches, generated native/protocol objects, build directories
  and distribution JARs. The tracked Gradle wrapper JAR remains included.
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
