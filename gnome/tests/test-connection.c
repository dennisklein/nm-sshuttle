/* SPDX-License-Identifier: MIT */
/* The editor's reads and writes of an NMConnection, without a display. */

#include <string.h>

#include "nm-sshuttle-connection.h"
#include "nm-sshuttle-validate.h"

static NMConnection *
new_connection(const char *permission_user)
{
    NMConnection *connection = nm_simple_connection_new();
    NMSetting *s_con = nm_setting_connection_new();
    NMSetting *s_ip4 = g_object_new(NM_TYPE_SETTING_IP4_CONFIG, NM_SETTING_IP_CONFIG_METHOD,
                                    NM_SETTING_IP4_CONFIG_METHOD_AUTO, NULL);

    /* What Settings has made of a new VPN before the editor is asked. */
    g_object_set(s_con, NM_SETTING_CONNECTION_ID, "corp", NM_SETTING_CONNECTION_TYPE,
                 NM_SETTING_VPN_SETTING_NAME, NM_SETTING_CONNECTION_AUTOCONNECT, TRUE, NULL);
    if (permission_user)
        nm_setting_connection_add_permission(NM_SETTING_CONNECTION(s_con), "user", permission_user,
                                             NULL);
    nm_connection_add_setting(connection, s_con);
    nm_connection_add_setting(connection, s_ip4);
    return connection;
}

static GHashTable *
form(void)
{
    GHashTable *data = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);

    g_hash_table_replace(data, g_strdup("remote"), g_strdup("corp"));
    g_hash_table_replace(data, g_strdup("local-user"), g_strdup("alice"));
    g_hash_table_replace(data, g_strdup("subnets"), g_strdup("10.0.0.0/8"));
    g_hash_table_replace(data, g_strdup("dns"), g_strdup("none"));
    return data;
}

static void
test_new_profile(void)
{
    g_autoptr(NMConnection) connection = new_connection("alice");
    g_autoptr(GHashTable) data = form();
    g_autoptr(GError) error = NULL;
    NMSettingVpn *s_vpn;
    NMSettingIPConfig *s_ip4, *s_ip6;

    g_assert_true(nmss_connection_update(connection, data, &error));
    g_assert_no_error(error);

    s_vpn = nm_connection_get_setting_vpn(connection);
    g_assert_nonnull(s_vpn);
    g_assert_cmpstr(nm_setting_vpn_get_service_type(s_vpn), ==, NMSS_SERVICE_TYPE);
    g_assert_true(nm_setting_vpn_get_persistent(s_vpn));
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "subnets"), ==, "10.0.0.0/8");
    g_assert_false(nm_setting_connection_get_autoconnect(
        nm_connection_get_setting_connection(connection)));

    s_ip4 = nm_connection_get_setting_ip4_config(connection);
    s_ip6 = nm_connection_get_setting_ip6_config(connection);
    g_assert_nonnull(s_ip6);
    g_assert_cmpint(nm_setting_ip_config_get_auto_route_ext_gw(s_ip4), ==, NM_TERNARY_FALSE);
    g_assert_cmpint(nm_setting_ip_config_get_auto_route_ext_gw(s_ip6), ==, NM_TERNARY_FALSE);
}

static void
test_existing_profile_keeps_unknown_keys(void)
{
    g_autoptr(NMConnection) connection = new_connection("alice");
    g_autoptr(GHashTable) data = form();
    g_autoptr(GHashTable) read = NULL;
    NMSettingVpn *s_vpn;

    g_hash_table_replace(data, g_strdup("exclude"), g_strdup("10.0.5.0/24"));
    g_hash_table_replace(data, g_strdup("from-a-newer-cli"), g_strdup("x"));
    g_assert_true(nmss_connection_update(connection, data, NULL));

    /* The form is read back, changed and written again, as Settings does. */
    read = nmss_connection_get_data(connection);
    g_assert_cmpstr(g_hash_table_lookup(read, "from-a-newer-cli"), ==, "x");
    g_hash_table_remove(read, "exclude");
    g_hash_table_replace(read, g_strdup("subnets"), g_strdup("172.20.0.0/16"));
    g_assert_true(nmss_connection_update(connection, read, NULL));

    s_vpn = nm_connection_get_setting_vpn(connection);
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "from-a-newer-cli"), ==, "x");
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "subnets"), ==, "172.20.0.0/16");
    g_assert_null(nm_setting_vpn_get_data_item(s_vpn, "exclude"));
}

static void
assert_refused(NMConnection *connection, NmssField field)
{
    g_autoptr(GHashTable) data = form();
    g_autoptr(GError) error = NULL;

    g_assert_false(nmss_connection_update(connection, data, &error));
    g_assert_true(g_error_matches(error, NMSS_VALIDATE_ERROR, (int) field));
    /* Nothing was written. */
    g_assert_null(nm_connection_get_setting_vpn(connection));
    g_assert_true(nm_setting_connection_get_autoconnect(
        nm_connection_get_setting_connection(connection)));
}

static void
test_all_users_is_refused(void)
{
    g_autoptr(NMConnection) connection = new_connection(NULL);

    assert_refused(connection, NMSS_FIELD_PERMISSIONS);
}

static void
test_another_user_is_refused(void)
{
    g_autoptr(NMConnection) connection = new_connection("bob");

    assert_refused(connection, NMSS_FIELD_PERMISSIONS);
}

static void
test_ipv4_method_is_refused(void)
{
    g_autoptr(NMConnection) connection = new_connection("alice");

    g_object_set(nm_connection_get_setting_ip4_config(connection), NM_SETTING_IP_CONFIG_METHOD,
                 NM_SETTING_IP4_CONFIG_METHOD_MANUAL, NULL);
    assert_refused(connection, NMSS_FIELD_IPV4_METHOD);
}

static void
test_missing_ip_settings_are_added(void)
{
    g_autoptr(NMConnection) connection = nm_simple_connection_new();
    g_autoptr(GHashTable) data = form();
    NMSetting *s_con = nm_setting_connection_new();

    g_object_set(s_con, NM_SETTING_CONNECTION_ID, "corp", NM_SETTING_CONNECTION_TYPE,
                 NM_SETTING_VPN_SETTING_NAME, NULL);
    nm_setting_connection_add_permission(NM_SETTING_CONNECTION(s_con), "user", "alice", NULL);
    nm_connection_add_setting(connection, s_con);

    g_assert_true(nmss_connection_update(connection, data, NULL));
    g_assert_cmpstr(nm_setting_ip_config_get_method(
                        nm_connection_get_setting_ip4_config(connection)),
                    ==, NM_SETTING_IP4_CONFIG_METHOD_AUTO);
    g_assert_cmpint(nm_setting_ip_config_get_auto_route_ext_gw(
                        nm_connection_get_setting_ip4_config(connection)),
                    ==, NM_TERNARY_FALSE);
}

int
main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/connection/new-profile", test_new_profile);
    g_test_add_func("/connection/keeps-unknown-keys", test_existing_profile_keeps_unknown_keys);
    g_test_add_func("/connection/all-users-is-refused", test_all_users_is_refused);
    g_test_add_func("/connection/another-user-is-refused", test_another_user_is_refused);
    g_test_add_func("/connection/ipv4-method-is-refused", test_ipv4_method_is_refused);
    g_test_add_func("/connection/missing-ip-settings", test_missing_ip_settings_are_added);
    return g_test_run();
}
