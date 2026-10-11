# SPDX-License-Identifier: MIT

Name:           nm-sshuttle-release
# Bump Version when nm-sshuttle.repo or the signing key changes.
Version:        1
Release:        1
Summary:        dnf repository configuration for nm-sshuttle's Fedora packages

License:        MIT
URL:            https://github.com/dennisklein/nm-sshuttle
Source0:        nm-sshuttle.repo
Source1:        RPM-GPG-KEY-nm-sshuttle

BuildArch:      noarch

%description
Configures the dnf repository at https://dennisklein.github.io/nm-sshuttle/,
which serves the nm-sshuttle packages signed by the nm-sshuttle project, and
installs the OpenPGP key that verifies its packages and metadata.

%prep

%build

%install
install -Dpm 0644 %{SOURCE0} %{buildroot}%{_sysconfdir}/yum.repos.d/nm-sshuttle.repo
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_sysconfdir}/pki/rpm-gpg/RPM-GPG-KEY-nm-sshuttle

%files
%config(noreplace) %{_sysconfdir}/yum.repos.d/nm-sshuttle.repo
%{_sysconfdir}/pki/rpm-gpg/RPM-GPG-KEY-nm-sshuttle

%changelog
* Sun Oct 11 2026 Dennis Klein <ilmt2000@googlemail.com> - 1-1
- Initial package
