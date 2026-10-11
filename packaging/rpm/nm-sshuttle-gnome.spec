# SPDX-License-Identifier: MIT
#
# The editor for GNOME Settings, from the same tarball as nm-sshuttle.spec. It
# has its own spec because it is compiled code and nm-sshuttle is noarch.
Name:           nm-sshuttle-gnome
Version:        0.1.0
Release:        1%{?dist}
Summary:        GNOME Settings editor for nm-sshuttle profiles
License:        MIT
URL:            https://github.com/dennisklein/nm-sshuttle
Source0:        %{url}/archive/v%{version}/nm-sshuttle-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  meson >= 0.64
BuildRequires:  python3-devel
BuildRequires:  pkgconfig(glib-2.0) >= 2.74
BuildRequires:  pkgconfig(gtk4)
BuildRequires:  pkgconfig(json-glib-1.0)
BuildRequires:  pkgconfig(libnm) >= 1.42
BuildRequires:  xorg-x11-server-Xvfb
BuildRequires:  xauth

Requires:       NetworkManager >= 1.42
Requires:       nm-sshuttle = %{version}

%description
Adds "sshuttle" to the VPN types that GNOME Settings can add and edit, so that
a profile can be made and changed there instead of with `nm-sshuttle add`. The
editor is a GTK4 library that GNOME Settings loads; it has no GTK3 counterpart
for nm-connection-editor yet.

Without this package GNOME Settings still lists and switches the profiles, but
cannot edit them.

%prep
%autosetup -n nm-sshuttle-%{version}

%build
export PATH=/usr/bin:$PATH
%meson -Dgnome-editor=enabled
%meson_build

%install
# Only the editor: the Python parts belong to nm-sshuttle.
%{__meson} install -C %{_vpath_builddir} --no-rebuild --destdir %{buildroot} --tags gnome-editor

%check
xvfb-run -a %{__meson} test -C %{_vpath_builddir} --no-rebuild --print-errorlogs --suite gnome-editor

%files
%license LICENSE
%{_libdir}/NetworkManager/libnm-gtk4-vpn-plugin-sshuttle-editor.so
%{_libdir}/NetworkManager/libnm-vpn-plugin-sshuttle.so

%changelog
* Sun Oct 11 2026 Dennis Klein <ilmt2000@googlemail.com> - 0.1.0-1
- Initial package
