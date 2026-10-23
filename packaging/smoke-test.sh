#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Install the nm-sshuttle packages from a repository site the way users do and
# check that they are signed and in place. Meant for a throwaway Fedora
# container.
#
# Usage: smoke-test.sh <site-url>
set -euo pipefail

base=${1:?usage: smoke-test.sh <site-url>}
base=${base%/}

curl -fsSL --retry 5 --retry-all-errors "$base/RPM-GPG-KEY-nm-sshuttle" -o /tmp/RPM-GPG-KEY-nm-sshuttle
rpm --import /tmp/RPM-GPG-KEY-nm-sshuttle
dnf -y install --setopt=localpkg_gpgcheck=1 "$base/nm-sshuttle-release.noarch.rpm"
# nm-sshuttle.repo points at the public site; follow the site under test
# instead. --allowerasing: the container has systemd-standalone-tmpfiles,
# which conflicts with the systemd that systemd-resolved needs; a real system
# has the full systemd. No weak dependencies: GTK's media codecs include
# openh264, which comes from a third-party host that a hosted runner fails to
# reach now and then, and nothing here uses them.
dnf -y install --allowerasing --setopt=install_weak_deps=False --setopt="nm-sshuttle.baseurl=$base/rpm/fedora/\$releasever/\$basearch/" \
  nm-sshuttle nm-sshuttle-gnome

rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}: %{SIGPGP:pgpsig}\n' \
  nm-sshuttle-release nm-sshuttle nm-sshuttle-gnome

# The files of both packages are in place, and the profile commands run.
nm-sshuttle --help > /dev/null
test -s /usr/lib/NetworkManager/VPN/nm-sshuttle-service.name
test -s /usr/lib64/NetworkManager/libnm-gtk4-vpn-plugin-sshuttle-editor.so
