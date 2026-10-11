/* SPDX-License-Identifier: MIT */

#include "nm-sshuttle-connection.h"

#include "nm-sshuttle-validate.h"

const char *const NMSS_FORM_KEYS[] = {
    "remote",     "local-user", "subnets", "exclude", "dns",    "dns-servers",
    "dns-domains", "method",     "gateway", "probe",   "fail-closed", NULL,
};

static void
copy_item(const char *key, const char *value, gpointer user_data)
{
    g_hash_table_replace(user_data, g_strdup(key), g_strdup(value));
}

GHashTable *
nmss_connection_get_data(NMConnection *connection)
{
    GHashTable *data = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
    NMSettingVpn *s_vpn = nm_connection_get_setting_vpn(connection);

    if (s_vpn)
        nm_setting_vpn_foreach_data_item(s_vpn, copy_item, data);
    return data;
}

/* "user:alice" for each entry of connection.permissions. */
static char **
get_permissions(NMSettingConnection *s_con)
{
    GPtrArray *perms = g_ptr_array_new();

    for (guint32 i = 0; s_con && i < nm_setting_connection_get_num_permissions(s_con); i++) {
        const char *type, *item;

        if (nm_setting_connection_get_permission(s_con, i, &type, &item, NULL))
            g_ptr_array_add(perms, g_strdup_printf("%s:%s", type, item));
    }
    g_ptr_array_add(perms, NULL);
    return (char **) g_ptr_array_free(perms, FALSE);
}

static NMSettingIPConfig *
get_ip_config(NMConnection *connection, GType type)
{
    NMSettingIPConfig *s_ip = NM_SETTING_IP_CONFIG(nm_connection_get_setting(connection, type));

    if (!s_ip) {
        s_ip = g_object_new(type, NM_SETTING_IP_CONFIG_METHOD, NM_SETTING_IP4_CONFIG_METHOD_AUTO,
                            NULL);
        nm_connection_add_setting(connection, NM_SETTING(s_ip));
    }
    return s_ip;
}

gboolean
nmss_connection_update(NMConnection *connection, GHashTable *data, GError **error)
{
    NMSettingConnection *s_con = nm_connection_get_setting_connection(connection);
    NMSettingIPConfig *s_ip4 = nm_connection_get_setting_ip4_config(connection);
    NMSettingVpn *s_vpn = nm_connection_get_setting_vpn(connection);
    g_auto(GStrv) permissions = get_permissions(s_con);
    NmssProfile profile = {
        .data = data,
        .permissions = (const char *const *) permissions,
        .persistent = TRUE,
        .ipv4_method = s_ip4 ? nm_setting_ip_config_get_method(s_ip4) : NULL,
    };
    GHashTableIter iter;
    gpointer key, value;

    if (!nmss_profile_validate(&profile, error))
        return FALSE;

    if (!s_vpn) {
        s_vpn = NM_SETTING_VPN(nm_setting_vpn_new());
        nm_connection_add_setting(connection, NM_SETTING(s_vpn));
    }
    g_object_set(s_vpn, NM_SETTING_VPN_SERVICE_TYPE, NMSS_SERVICE_TYPE, NM_SETTING_VPN_PERSISTENT,
                 TRUE, NULL);
    {
        guint n_old = 0;
        g_auto(GStrv) old_keys = g_strdupv((char **) nm_setting_vpn_get_data_keys(s_vpn, &n_old));

        for (guint i = 0; i < n_old; i++)
            nm_setting_vpn_remove_data_item(s_vpn, old_keys[i]);
    }
    g_hash_table_iter_init(&iter, data);
    while (g_hash_table_iter_next(&iter, &key, &value))
        nm_setting_vpn_add_data_item(s_vpn, key, value);

    if (s_con)
        g_object_set(s_con, NM_SETTING_CONNECTION_AUTOCONNECT, FALSE, NULL);
    /* No Settings page writes these. NM's host route to the first hop would
     * point at the old router after a roam (design 4.1). */
    g_object_set(get_ip_config(connection, NM_TYPE_SETTING_IP4_CONFIG),
                 NM_SETTING_IP_CONFIG_AUTO_ROUTE_EXT_GW, NM_TERNARY_FALSE, NULL);
    g_object_set(get_ip_config(connection, NM_TYPE_SETTING_IP6_CONFIG),
                 NM_SETTING_IP_CONFIG_AUTO_ROUTE_EXT_GW, NM_TERNARY_FALSE, NULL);
    return TRUE;
}
