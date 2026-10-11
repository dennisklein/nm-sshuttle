# SPDX-License-Identifier: MIT
#
# The editor for GNOME Settings (docs/later/gnome-editor.md).

feature = get_option('gnome-editor')
glib = dependency('glib-2.0', version: '>= 2.74', required: feature)
libnm = dependency('libnm', version: '>= 1.42', required: feature)
if not (glib.found() and libnm.found())
  subdir_done()
endif
# The rest of the project is Python and needs no compiler.
if not add_languages('c', required: feature)
  subdir_done()
endif

c_args = [
  '-DGETTEXT_PACKAGE="nm-sshuttle"',
  '-DGLIB_VERSION_MIN_REQUIRED=GLIB_VERSION_2_74',
]
inc = include_directories('.')

# The rules of the profile and the writing of a connection, without any GTK,
# so that the tests can run without a display.
validate = static_library('nm-sshuttle-validate', 'nm-sshuttle-validate.c',
                          c_args: c_args, dependencies: glib, pic: true)

connection = static_library('nm-sshuttle-connection', 'nm-sshuttle-connection.c',
                            c_args: c_args, dependencies: libnm, link_with: validate, pic: true)

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
