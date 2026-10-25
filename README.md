# Cyberdeck Debian packages

Service code and its Debian recipe live in
[ardangelo/cyberdeck-service](https://github.com/ardangelo/cyberdeck-service),
on `master`. Its `mobileapp` and `src/nanopb` entries remain real Git submodules.
This repository owns build orchestration; do not copy service code or maintain
another service `debian/` here.

The package service targets Debian trixie on RISC-V, configuration
`k230_canmv_01studio_defconfig`. `source-lock.json` pins all source commits,
tool downloads, build-image digests and the Debian dependency snapshot.
Four binaries come from three quilt source packages; source and license
archives remain on immutable GitHub Releases, while Pages serves binary APT.

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

Source staging requires Git and Python 3.10 or newer. Export into a fresh
throwaway directory. Never use an existing development worktree or its compiled
outputs as a package input.
Extraction rejects path traversal, escaping symlinks and non-source special
entries before writing, without relying on version-specific tar filters.

Run the isolated behavioral suite from this repository. Tests that exercise
signatures and Debian indexes require GnuPG, OpenSSL, `dpkg-dev` and `apt-utils`:

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

## Build and verify a cohort

Use a Linux x86-64 machine with Docker, Git and Python. These commands fetch
public sources and build their own locked containers; they do not consume
development worktrees, local JARs or previously built packages:

```sh
python3 scripts/build-packages.py --package status-ready --release-number 1 --output output/release-1
python3 scripts/build-packages.py --package cyberdeck-service --release-number 1 --output output/release-1
python3 scripts/build-packages.py --package sharp-drm --release-number 1 --output output/release-1
python3 scripts/verify-artifacts.py --artifacts output/release-1 --release-number 1 --output output/verified-1
sudo python3 scripts/smoke-packages.py --input output/verified-1 --output output/smoke-1
```

The selected package output and verification/evidence directories must be
fresh. The three builds may run independently. Each creates its source package,
extracts the `.dsc`, and compiles that extracted source with ordinary build
dependency checks. Corresponding Maven, Cargo and native-library source reports
are separate Release assets, not hidden inside a container or an Actions cache.

The service version is `0.1+cyberdeck.N-1`; readiness is
`1.0+cyberdeck.N-1`. Sharp and its exact-version marker use
`1.7+cyberdeck.N+abi.H-1`, where `H` is the full 64-character kernel ABI hash.
Publication requires an unpadded positive decimal `N` above every published
`packages-vN`. Only CI permits `--release-number 0 --development`, with
`CI=true`; those development builds cannot be archived.

The kernel builder reconstructs the configured SDK kernel, including patches,
fragment, generated headers, host tools and `Module.symvers`; it does not use
generic headers or `modules_prepare` as a substitute. It downloads and verifies
the locked Xuantie archive rather than requiring an untracked build-context
file. `scripts/kernel-context.py --sdk-root SDK --conf
k230_canmv_01studio_defconfig` resolves the prepared kernel and emits its context.
The ABI covers the kernel source revision/release, actual configuration and
symbols, authored kernel inputs and compiler identity/bytes. It excludes SDK
commit, build paths, package versions and timestamps.

Both ABI-specific packages reject installation or upgrade unless the
image-owned `/usr/share/k230/kernel-abi` contains exactly their expected hash
with a final newline. Neither package owns that file or supplies a kernel.
The marker carries its expected context at
`/usr/share/k230-kernel-abi/kernel-context.json`. A different ABI requires a
different image and APT suite even if `kernel_release` stays the same.

The target smoke requires `mmdebstrap`, the Debian archive keyring, static
RISC-V QEMU with working binfmt registration, a RISC-V C++ compiler, GnuPG,
native `systemctl`, kmod, root privileges and permission to mount read-only `/proc`.
Use a capable Linux host or container; there is no mountless PRoot fallback.
It extracts the signed base archives, completes mmdebstrap's essential stage
and requires a configured, dependency-consistent dpkg database before installing
the cohort. The essential stage uses upstream mmdebstrap's bootstrap-only
`dpkg --install --force-depends` ordering; the cohort uses normal APT checks.
Hardware services are inhibited before any post-install script runs.
It exercises readiness, ABI guards, module metadata and packaged-JAR JNI
initialization. QEMU `/dev/null` initialization is not a UART, display or
hardware boot test.

## Immutable Releases and APT publication

`ci.yml` builds and verifies pull requests and `main` without signing secrets.
Build failures include captured command diagnostics in public check annotations;
full job logs remain in Actions. No signing secrets enter these build jobs.
`release.yml` accepts a manual positive release number on `main`, runs the same
three unprivileged builds and target verification, then enters `apt-publish`
only for archival. `archive-release.py` creates a draft `packages-vN` at the
checked-out main commit, signs the manifest, verifies uploaded draft assets
through authenticated asset IDs, and publishes only the complete cohort.
It checks immutable status and public asset bytes after publication.

Every archive retains all four binaries, the three complete Debian sources,
corresponding-source reports, `.changes`, `.buildinfo`, kernel context, signed
manifest and `snapshot-Packages`. The golden index uses final paths
`pool/trixie-k230-H/packages-vN/<deb-basename>` and is never regenerated for an
existing release. Conflicting drafts or same-version/different-byte releases
fail without overwriting assets. An identical immutable archive is idempotent;
changed source commits or bytes require a new release number.

`publish.yml` reconstructs Pages from immutable public Releases, not a previous
deployment, cache, state branch or Actions artifact. It verifies the signed
manifests, package tuples/bytes, context and golden indexes before staging.

The release workflow calls it only after successful archival; no `workflow_run`
privilege transition or fork-artifact handoff is used. Assembly and deployment
remain separate jobs with their own least-privilege tokens and environments.

Generated suites:

- `trixie-k230-H`: the live suite, retaining every active version for this ABI.
- `trixie-k230-H-release-N`: the fixed image cohort, with byte-identical golden
  `Packages` and deterministic `Packages.gz`.

Both use fresh signed `Release`, `InRelease` and `Release.gpg` metadata with a
14-day validity window. The Monday/Thursday refresh changes freshness and
signatures, not snapshot package/index bytes. There is no `trusted=yes`,
Cyberdeck expiry bypass or advertised By-Hash retention. A publication above
900 MiB fails before deployment; immutable source archives stay on Releases
and the catalog links them.

Append reviewed `{ "tag": "packages-vN", "reason": "..." }` entries to
`withdrawn-releases.json` to withdraw a cohort. Do not edit or remove earlier
entries. Publication removes its live-index entries and snapshot suite but
retains pool bytes and Release assets. A locked image must fail on withdrawal,
not fall back to a newer/live cohort.

Run publish-only after a deployment failure or a missed refresh; do not rebuild
binaries under an existing version:

```sh
gh workflow run publish.yml --repo ardangelo/cyberdeck-packages --ref main
```

The manual `dry_run` option validates and assembles without environments,
signing secrets or deployment. Its unsigned output is not deployable APT.
The public endpoint is `https://ardangelo.github.io/cyberdeck-packages/`.
Its availability and first immutable release must be proven before the image
repository removes its current local packaging pipeline.

## Maintainer setup and signing custody

Configure GitHub manually before the first release:

1. Keep this repository public with default branch `main`. Enable immutable
   Releases before creating `packages-v1`; select GitHub Actions as the Pages
   source.
2. Enable Actions with read-only default token permissions. Allow the pinned
   official actions used here; do not grant PR creation/approval permission.
3. Create `apt-publish` and `github-pages` environments, each restricted to
   **branch `main` only**, not tags or PR refs. Use no required reviewer or wait
   timer for unattended publication. Disable bypass where available.
4. After CI has registered its checks, protect `main` against deletion and
   force pushes, require pull requests, and require `Build status-ready`,
   `Build cyberdeck-service`, `Build sharp-drm`, and
   `Verify packages and smoke` from `Package CI`. Do not routinely bypass them.
5. Give only `apt-publish` the secrets `APT_GPG_PRIVATE_KEY` and
   `APT_GPG_PASSPHRASE`, plus variable `APT_GPG_SIGNING_FINGERPRINT`.
   `github-pages` receives no signing key. Release archival alone has
   `contents: write`; assembly has `contents: read`; deployment alone has
   `pages: write` and `id-token: write`.

Create the real repository-specific key on a trusted maintainer machine, not
in Actions or the package-build container. Use Bash, an interactive secure
pinentry, a password manager and a mounted offline backup medium. Do not enable
shell tracing. The following commands prompt for the strong key passphrase;
they never put it in an argument or shell variable:

```bash
set -euo pipefail
umask 077
export GNUPGHOME="$(mktemp -d)"
gpg --quick-generate-key 'Cyberdeck APT Archive <dangeloandrew@outlook.com>' ed25519 cert 2y
gpg --with-colons --fingerprint --list-secret-keys
read -r -p 'Full uppercase primary fingerprint: ' PRIMARY
gpg --quick-add-key "$PRIMARY" ed25519 sign 2y
gpg --with-subkey-fingerprint --list-secret-keys "$PRIMARY"
read -r -p 'Mounted offline backup directory: ' OFFLINE_BACKUP
test -d "$OFFLINE_BACKUP"
gpg --armor --export-secret-keys "$PRIMARY" |
  gpg --symmetric --cipher-algo AES256 --output "$OFFLINE_BACKUP/cyberdeck-full-key.asc.gpg"
cp "$GNUPGHOME/openpgp-revocs.d/$PRIMARY.rev" "$OFFLINE_BACKUP/"
mkdir -p keys
gpg --armor --export "$PRIMARY" > keys/cyberdeck-archive.asc
python3 - "$PRIMARY" > repository.json <<'PY'
import json, re, sys
fingerprint = sys.argv[1]
assert re.fullmatch(r'[0-9A-F]{40}', fingerprint)
print(json.dumps({'schema_version': 1,
                  'repository': 'ardangelo/cyberdeck-packages',
                  'signing_fingerprint': fingerprint}, indent=2))
PY
chmod 0644 keys/cyberdeck-archive.asc repository.json
gpg --armor --export-secret-subkeys "$PRIMARY" |
  base64 -w0 > "$GNUPGHOME/actions-subkeys.base64"
```

Verify the encrypted offline backup can be recovered securely before removing
the original key home. Keep the revocation certificate offline too. Disconnect
the backup medium before provisioning Actions.

In GitHub's `apt-publish` environment UI, paste the complete local
`actions-subkeys.base64` contents into `APT_GPG_PRIVATE_KEY` using a trusted
editor/clipboard, not terminal output, chat or logs. Set the key passphrase
separately as `APT_GPG_PASSPHRASE`. Set `APT_GPG_SIGNING_FINGERPRINT` to the full
uppercase **signing subkey** fingerprint displayed above, not the primary.
Only the public certificate and `repository.json` belong in Git.
Once backup and environment provisioning are verified:

```bash
gpgconf --homedir "$GNUPGHOME" --kill gpg-agent
rm -rf -- "$GNUPGHOME"
unset GNUPGHOME PRIMARY OFFLINE_BACKUP
```

The signing helper imports secret subkeys into a fresh mode-0700 home under
`RUNNER_TEMP`, checks the configured primary/subkey, signs with an explicit
`SUBKEY!` and a passphrase pipe, verifies using public material, and stops the
agent/removes the private home on success or failure. Neither private export
nor passphrase belongs in an artifact.

After green clean-host CI and verified key provisioning, dispatch the first
release from `main` with `release_number=1`. Observe the build, target smoke,
immutable archive and Pages deployment; verify both fixed and live APT suites
before changing image consumers. A publish-only rerun must reconstruct the
same retained cohorts from Releases after local generated state is removed.

### Expiry, replacement and compromise

Renew the existing primary/signing-subkey expiry with the offline primary
before the two-year expiry. Export and review the renewed public certificate,
refresh the protected secret-subkey material, and update image/device trust
inputs. Existing devices need the renewed certificate before their old
certificate expires. A missed 14-day metadata refresh is repaired by
publish-only, never by disabling APT freshness checks.

For replacement, authenticate an overlap public-key bundle with the old key
and perform an explicit operator trust update on devices. Keep old public
certificates for historical manifest verification. Add an optional
`historical_keys` array to `repository.json`, each entry containing
`fingerprint` and `max_release_number`, to bound the releases a retired primary
may authenticate. The new primary remains `signing_fingerprint`; never let a
retired key authorize later release numbers or blindly trust a certificate
downloaded from the repository it is meant to authenticate.

On compromise, disable signing/publication, replace trust out of band, review
withdrawals and issue higher-version fixed releases. Retain immutable archives
for audit. Back up the artifacts and key material offline; neither an old
Pages deployment nor an expiring Actions artifact is disaster recovery.

## Authenticated APT smoke

`scripts/smoke-apt.py` accepts `--uri`, `--suite`, `--key` and
`--expected-manifest`; it verifies the sibling manifest `.asc` signature with
`repository.json`. `--repository-config`, `--signature`, `--context` and
`--golden-index` may supply explicit files, still checked against the signed
manifest. `--output` retains public JSON evidence. It uses an isolated target
rootfs, normal signature/freshness validation, effective candidates, transaction
simulation, complete download/hash inspection and an actual offline install.
Run it once for the manifest's snapshot suite and once for its live suite.
Only loopback HTTP is permitted for local signed fixtures; hosted use requires
HTTPS. No signing secret is needed.
