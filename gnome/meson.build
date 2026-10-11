# SPDX-License-Identifier: MIT
#
# The editor for GNOME Settings (docs/later/gnome-editor.md): a base plugin
# that links libnm only, and a GTK4 library with the form.

feature = get_option('gnome-editor')
glib = dependency('glib-2.0', version: '>= 2.74', required: feature)
gmodule = dependency('gmodule-2.0', required: feature)
libnm = dependency('libnm', version: '>= 1.42', required: feature)
gtk4 = dependency('gtk4', required: feature)
if not (glib.found() and gmodule.found() and libnm.found() and gtk4.found())
  subdir_done()
endif
# The rest of the project is Python and needs no compiler.
if not add_languages('c', required: feature)
  subdir_done()
endif

gnome = import('gnome')
cc = meson.get_compiler('c')
dl = cc.find_library('dl', required: false)

# NetworkManager looks for an editor library here, whatever the prefix. Not
# every libnm.pc says where (Fedora's does not); the library directory is
# where the distributions put it.
plugindir = get_option('nmplugindir')
if plugindir == ''
  plugindir = libnm.get_variable(pkgconfig: 'plugindir',
                                 default_value: get_option('prefix') / get_option('libdir') / 'NetworkManager')
endif

c_args = [
  '-DGETTEXT_PACKAGE="nm-sshuttle"',
  '-DLOCALEDIR="@0@"'.format(get_option('prefix') / get_option('localedir')),
  '-DGLIB_VERSION_MIN_REQUIRED=GLIB_VERSION_2_74',
]
inc = include_directories('.')

# The rules of the profile and the writing of a connection, without any GTK,
# so that the tests can run without a display.
validate = static_library('nm-sshuttle-validate', 'nm-sshuttle-validate.c',
                          c_args: c_args, dependencies: glib, pic: true)
connection = static_library('nm-sshuttle-connection', 'nm-sshuttle-connection.c',
                            c_args: c_args, dependencies: libnm, link_with: validate, pic: true)

# libnm-vpn-plugin-sshuttle.so is what the .name file's [libnm] plugin= names.
base_plugin = shared_module('nm-vpn-plugin-sshuttle', 'nm-sshuttle-editor-plugin.c',
                            name_prefix: 'lib', c_args: c_args,
                            dependencies: [libnm, gmodule, dl],
                            install: true, install_dir: plugindir)

resources = gnome.compile_resources('nm-sshuttle-editor-resources',
                                    'nm-sshuttle-editor.gresource.xml',
                                    c_name: 'nmss_editor')
editor_plugin = shared_module('nm-gtk4-vpn-plugin-sshuttle-editor',
                              'nm-sshuttle-editor.c', resources,
                              name_prefix: 'lib', c_args: c_args,
                              dependencies: [libnm, gtk4, gmodule],
                              link_with: [connection, validate],
                              install: true, install_dir: plugindir)

json_glib = dependency('json-glib-1.0', required: false)
if json_glib.found()
  test('profile cases in C',
       executable('test-validate', 'tests/test-validate.c', c_args: c_args,
                  dependencies: [glib, json_glib], link_with: validate,
                  include_directories: inc),
       args: [meson.project_source_root() / 'tests' / 'profile-cases.json'])
endif
test('connection writes',
     executable('test-connection', 'tests/test-connection.c', c_args: c_args,
                dependencies: libnm, link_with: [connection, validate],
                include_directories: inc))
test('no GTK4 editor in a GTK3 process',
     executable('test-gtk3', 'tests/test-gtk3.c', c_args: c_args, dependencies: [libnm, gmodule],
                export_dynamic: true, include_directories: inc),
     args: [base_plugin.full_path()])
# Needs a display, so run it under xvfb-run; without one it exits with 77, a skip.
test('editor form',
     executable('test-editor', 'tests/test-editor.c', c_args: c_args,
                dependencies: [libnm, gtk4], link_with: [connection, validate],
                include_directories: inc),
     args: [base_plugin.full_path()], depends: [editor_plugin],
     env: {'GTK_A11Y': 'none', 'GSETTINGS_BACKEND': 'memory', 'GSK_RENDERER': 'cairo'})
