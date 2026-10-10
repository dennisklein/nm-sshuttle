# SPDX-License-Identifier: MIT
%global nm_libdir %{_prefix}/lib/NetworkManager

Name:           nm-sshuttle
Version:        0.1.0~dev0
Release:        1%{?dist}
Summary:        NetworkManager VPN plugin that tunnels with sshuttle
License:        MIT
URL:            https://github.com/dennisklein/nm-sshuttle
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  meson >= 0.64
BuildRequires:  python3-devel
BuildRequires:  python3-gobject-base
BuildRequires:  python3-pytest
BuildRequires:  systemd-rpm-macros

Requires:       NetworkManager >= 1.42
Requires:       python3 >= 3.10
Requires:       python3-gobject-base
Requires:       sshuttle >= 1.3.1
Requires:       nftables
Requires:       iproute
Requires:       util-linux
Requires:       openssh-clients
Requires:       systemd-resolved
Requires:       dbus-common

%description
nm-sshuttle runs sshuttle as a NetworkManager VPN connection. The tunnel is
brought up, supervised and torn down by a small root service. ssh runs as the
logged-in user, so the user's keys, agent and ~/.ssh/config apply.

%prep
%autosetup -n %{name}-%{version}

%build
# Meson bakes the interpreter's path into the scripts' shebangs. Where /usr/sbin
# comes first in PATH it would pick /usr/sbin/python3, which is not a provider.
export PATH=/usr/bin:$PATH
%meson
%meson_build

%install
%meson_install

%check
%meson_test

%post
# Apply the conf.d snippet that leaves nmss0 unmanaged, unless a VPN is active.
%{_libexecdir}/nm-sshuttle/nm-sshuttle post-install || :

%files
%license LICENSE
%doc README.md docs/user-guide.md
%{_bindir}/nm-sshuttle
%{python3_sitelib}/nm_sshuttle/
%{_libexecdir}/nm-sshuttle/
%{nm_libdir}/VPN/nm-sshuttle-service.name
%{nm_libdir}/conf.d/90-nm-sshuttle.conf
%{_datadir}/dbus-1/system.d/nm-sshuttle.conf
%{_datadir}/dbus-1/system-services/org.freedesktop.NetworkManager.sshuttle.service
%{_unitdir}/nm-sshuttle.service
%{_unitdir}/nm-sshuttle-tunnel.service

%changelog
* Sat Oct 10 2026 Dennis Klein <ilmt2000@googlemail.com> - 0.1.0~dev0-1
- Initial package
