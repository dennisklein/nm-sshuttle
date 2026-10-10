# SPDX-License-Identifier: MIT
#
#   meson setup build --prefix=/usr
#   meson install -C build
#   nm-sshuttle post-install      (as root, once the files are in place)
#
# Paths follow design §4.1. NetworkManager's directories are fixed under
# /usr/lib, whatever the prefix: NM looks for VPN plugins and its packaged
# conf.d nowhere else.
project('nm-sshuttle', version: '0.1.0.dev0', license: 'MIT', meson_version: '>= 0.64')

python = import('python').find_installation('python3')
prefix = get_option('prefix')
libexecdir = prefix / get_option('libexecdir') / 'nm-sshuttle'
datadir = prefix / get_option('datadir')

systemd_unitdir = get_option('systemdsystemunitdir')
if systemd_unitdir == ''
  systemd_unitdir = '/usr/lib/systemd/system'
endif
nm_libdir = get_option('nmlibdir')

subst = configuration_data()
subst.set('LIBEXECDIR', libexecdir)
subst.set('PYTHON', python.full_path())

python.install_sources(
  'nm_sshuttle/__init__.py',
  'nm_sshuttle/__main__.py',
  'nm_sshuttle/cli.py',
  'nm_sshuttle/const.py',
  'nm_sshuttle/guard.py',
  'nm_sshuttle/profile.py',
  'nm_sshuttle/service.py',
  'nm_sshuttle/supervisor.py',
  'nm_sshuttle/tunnel.py',
  subdir: 'nm_sshuttle',
)

foreach script : ['nm-sshuttle', 'nm-sshuttle-service']
  configure_file(input: 'data' / script + '.in', output: script, configuration: subst,
                 install: true, install_dir: libexecdir, install_mode: 'rwxr-xr-x')
endforeach
install_data('data/nm-sshuttle-activate', 'data/nm-sshuttle-auth-dialog',
             install_dir: libexecdir, install_mode: 'rwxr-xr-x')

configure_file(input: 'data/nm-sshuttle-service.name.in', output: 'nm-sshuttle-service.name',
               configuration: subst, install: true, install_dir: nm_libdir / 'VPN')
install_data('data/90-nm-sshuttle.conf', install_dir: nm_libdir / 'conf.d')

install_data('data/nm-sshuttle.conf', install_dir: datadir / 'dbus-1' / 'system.d')
configure_file(input: 'data/org.freedesktop.NetworkManager.sshuttle.service.in',
               output: 'org.freedesktop.NetworkManager.sshuttle.service',
               configuration: subst, install: true,
               install_dir: datadir / 'dbus-1' / 'system-services')

foreach unit : ['nm-sshuttle.service', 'nm-sshuttle-tunnel.service']
  configure_file(input: 'data' / unit + '.in', output: unit, configuration: subst,
                 install: true, install_dir: systemd_unitdir)
endforeach

test('unit tests', python, args: ['-m', 'pytest', '-q', meson.project_source_root() / 'tests'],
     workdir: meson.project_source_root())
