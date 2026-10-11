/* SPDX-License-Identifier: MIT */
#ifndef NM_SSHUTTLE_EDITOR_H
#define NM_SSHUTTLE_EDITOR_H

#include <NetworkManager.h>

G_BEGIN_DECLS

/*
 * The factory libnm-vpn-plugin-sshuttle.so looks up in this library. The
 * name has to match the one in nm-sshuttle-editor-plugin.c.
 */
G_MODULE_EXPORT NMVpnEditor *nm_vpn_editor_factory_sshuttle(NMVpnEditorPlugin *editor_plugin,
                                                            NMConnection      *connection,
                                                            GError           **error);

G_END_DECLS

#endif
