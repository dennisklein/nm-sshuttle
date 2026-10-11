/* SPDX-License-Identifier: MIT */
/*
 * What the editor reads from and writes to an NMConnection, without any
 * widgets, so that a test can drive it headless.
 */
#ifndef NM_SSHUTTLE_CONNECTION_H
#define NM_SSHUTTLE_CONNECTION_H

#include <NetworkManager.h>

G_BEGIN_DECLS

#define NMSS_SERVICE_TYPE "org.freedesktop.NetworkManager.sshuttle"

/* The keys of the form; every other vpn.data key is kept as it is. */
extern const char *const NMSS_FORM_KEYS[];

/* A copy of the connection's vpn.data (char * to char *), empty when there is none. */
GHashTable *nmss_connection_get_data(NMConnection *connection);

/*
 * Validates the profile that @data and the connection's other settings make
 * up, and writes it: vpn.data as it is in @data (keys not in it are removed),
 * vpn.persistent, connection.autoconnect, and auto-route-ext-gw off for IPv4
 * and IPv6. Writes nothing and returns FALSE when the plugin would refuse
 * the profile.
 */
gboolean nmss_connection_update(NMConnection *connection, GHashTable *data, GError **error);

G_END_DECLS

#endif
