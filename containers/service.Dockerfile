# Pass only the linux/amd64 digest and exact tooling entries from source-lock.json.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG DEBIAN_SNAPSHOT
ARG JDK17_URL
ARG JDK17_SHA256
ARG ANDROID_COMMANDLINE_URL
ARG ANDROID_COMMANDLINE_SHA256
ARG ANDROID_PLATFORM_URL
ARG ANDROID_PLATFORM_SHA256

RUN test "$DEBIAN_SNAPSHOT" = 20250809T000000Z \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && printf '%s\n' 'Package: *' 'Pin: origin snapshot.debian.org' \
       'Pin-Priority: 1001' > /etc/apt/preferences.d/build-snapshot \
    && printf '%s\n' \
       'Types: deb' \
       "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
       'Suites: trixie' 'Components: main' \
       'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
       'Check-Valid-Until: no' > /etc/apt/sources.list.d/debian.sources \
    && apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --allow-downgrades --no-install-recommends ca-certificates \
    && printf '%s\n' \
       'Types: deb' \
       "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
       'Suites: trixie' 'Components: main' \
       'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
       'Check-Valid-Until: no' > /etc/apt/sources.list.d/debian.sources \
    && dpkg --add-architecture riscv64 \
    && apt-get -o APT::Update::Error-Mode=any update \
    && apt-get install -y --allow-downgrades --no-install-recommends \
       ca-certificates curl git debhelper dpkg-dev fakeroot \
       crossbuild-essential-riscv64 openjdk-21-jdk-headless \
       protobuf-compiler python3 python3-protobuf unzip xz-utils \
       libc6:riscv64 libstdc++6:riscv64 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    test -n "$JDK17_URL"; test -n "$JDK17_SHA256"; \
    test -n "$ANDROID_COMMANDLINE_URL"; test -n "$ANDROID_COMMANDLINE_SHA256"; \
    test -n "$ANDROID_PLATFORM_URL"; test -n "$ANDROID_PLATFORM_SHA256"; \
    curl --fail --location --proto '=https' --tlsv1.2 "$JDK17_URL" -o /tmp/jdk.tar.gz; \
    printf '%s  %s\n' "$JDK17_SHA256" /tmp/jdk.tar.gz | sha256sum --check --strict; \
    mkdir -p /opt/jdk-17; \
    tar -xzf /tmp/jdk.tar.gz --strip-components=1 -C /opt/jdk-17; \
    curl --fail --location --proto '=https' --tlsv1.2 "$ANDROID_COMMANDLINE_URL" -o /tmp/android-tools.zip; \
    printf '%s  %s\n' "$ANDROID_COMMANDLINE_SHA256" /tmp/android-tools.zip | sha256sum --check --strict; \
    mkdir -p /opt/android-sdk/cmdline-tools; \
    unzip -q /tmp/android-tools.zip -d /tmp/android-tools; \
    mv /tmp/android-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest; \
    curl --fail --location --proto '=https' --tlsv1.2 "$ANDROID_PLATFORM_URL" -o /tmp/android-platform.zip; \
    printf '%s  %s\n' "$ANDROID_PLATFORM_SHA256" /tmp/android-platform.zip | sha256sum --check --strict; \
    mkdir -p /opt/android-sdk/platforms; \
    unzip -q /tmp/android-platform.zip -d /opt/android-sdk/platforms; \
    test -f /opt/android-sdk/platforms/android-36/android.jar; \
    rm -rf /tmp/jdk.tar.gz /tmp/android-tools.zip /tmp/android-tools /tmp/android-platform.zip; \
    mkdir -p /opt/gradle-home; \
    printf '%s\n' \
      'org.gradle.java.installations.auto-download=false' \
      'org.gradle.java.installations.auto-detect=false' \
      'org.gradle.java.installations.paths=/opt/jdk-17,/usr/lib/jvm/java-21-openjdk-amd64' \
      > /opt/gradle-home/gradle.properties

ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    GRADLE_USER_HOME=/opt/gradle-home
WORKDIR /build
