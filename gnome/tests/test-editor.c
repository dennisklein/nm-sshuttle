/* SPDX-License-Identifier: MIT */
/*
 * Loads the plugin the way libnm does and drives the form it hands out.
 * Needs a display (xvfb-run); skips without one.
 *
 *   test-editor PLUGIN.so
 */

#include <gtk/gtk.h>

#include "nm-sshuttle-connection.h"
#include "nm-sshuttle-validate.h"

static const char *plugin_path;

static GtkWidget *
find(GtkWidget *widget, const char *name)
{
    for (GtkWidget *child = gtk_widget_get_first_child(widget); child;
         child = gtk_widget_get_next_sibling(child)) {
        GtkWidget *found;

        if (g_str_equal(gtk_widget_get_name(child), name))
            return child;
        found = find(child, name);
        if (found)
            return found;
    }
    return NULL;
}

typedef struct {
    NMVpnEditorPlugin *plugin;
    NMConnection *connection;
    NMVpnEditor *editor;
    GtkWidget *form;
    guint changed;
} Fixture;

static NMConnection *
new_connection(const char *user)
{
    NMConnection *connection = nm_simple_connection_new();
    NMSetting *s_con = nm_setting_connection_new();
    NMSetting *s_ip4 = g_object_new(NM_TYPE_SETTING_IP4_CONFIG, NM_SETTING_IP_CONFIG_METHOD,
                                    NM_SETTING_IP4_CONFIG_METHOD_AUTO, NULL);

    g_object_set(s_con, NM_SETTING_CONNECTION_ID, "corp", NM_SETTING_CONNECTION_TYPE,
                 NM_SETTING_VPN_SETTING_NAME, NULL);
    if (user)
        nm_setting_connection_add_permission(NM_SETTING_CONNECTION(s_con), "user", user, NULL);
    nm_connection_add_setting(connection, s_con);
    nm_connection_add_setting(connection, s_ip4);
    return connection;
}

static NMConnection *
cli_connection(void)
{
    NMConnection *connection = new_connection(g_get_user_name());
    NMSetting *s_vpn = nm_setting_vpn_new();

    /* What `nm-sshuttle add` makes, plus a key and a method of a newer version. */
    g_object_set(s_vpn, NM_SETTING_VPN_SERVICE_TYPE, NMSS_SERVICE_TYPE, NM_SETTING_VPN_PERSISTENT,
                 TRUE, NULL);
    nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), "remote", "corp");
    nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), "local-user", g_get_user_name());
    nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), "subnets", "10.0.0.0/8");
    nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), "method", "tproxy");
    nm_setting_vpn_add_data_item(NM_SETTING_VPN(s_vpn), "from-a-newer-cli", "x");
    nm_connection_add_setting(connection, s_vpn);
    return connection;
}

static void
count_changed(NMVpnEditor *editor, gpointer user_data)
{
    (*(guint *) user_data)++;
}

static void
setup(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;

    f->plugin = nm_vpn_editor_plugin_load_from_file(plugin_path, NMSS_SERVICE_TYPE, -1, NULL, NULL,
                                                    &error);
    g_assert_no_error(error);
    g_assert_nonnull(f->plugin);
    f->connection = g_str_equal(user_data, "cli") ? cli_connection()
                                                   : new_connection(g_get_user_name());
    f->editor = nm_vpn_editor_plugin_get_editor(f->plugin, f->connection, &error);
    g_assert_no_error(error);
    g_assert_nonnull(f->editor);
    f->form = GTK_WIDGET(nm_vpn_editor_get_widget(f->editor));
    g_assert_true(GTK_IS_WIDGET(f->form));
    g_signal_connect(f->editor, "changed", G_CALLBACK(count_changed), &f->changed);
}

static void
teardown(Fixture *f, gconstpointer user_data)
{
    g_clear_object(&f->editor);
    g_clear_object(&f->connection);
    g_clear_object(&f->plugin);
}

static void
type(Fixture *f, const char *name, const char *text)
{
    GtkWidget *entry = find(f->form, name);

    g_assert_nonnull(entry);
    gtk_editable_set_text(GTK_EDITABLE(entry), text);
}

static void
fill_minimum(Fixture *f)
{
    type(f, "remote", "corp");
    type(f, "subnets", "10.0.0.0/8, 172.20.0.0/16");
}

static void
test_names_the_service(Fixture *f, gconstpointer user_data)
{
    g_autofree char *name = NULL;
    g_autofree char *service = NULL;

    g_object_get(f->plugin, NM_VPN_EDITOR_PLUGIN_NAME, &name, NM_VPN_EDITOR_PLUGIN_SERVICE,
                 &service, NULL);
    g_assert_cmpstr(name, ==, "sshuttle");
    g_assert_cmpstr(service, ==, NMSS_SERVICE_TYPE);
}

static void
test_new_profile_needs_input(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;

    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_error(error, NMSS_VALIDATE_ERROR, NMSS_FIELD_REMOTE);
    g_assert_null(nm_connection_get_setting_vpn(f->connection));
}

static void
test_new_profile_is_written(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;
    NMSettingVpn *s_vpn;

    fill_minimum(f);
    g_assert_cmpuint(f->changed, >, 0);
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_no_error(error);

    s_vpn = nm_connection_get_setting_vpn(f->connection);
    g_assert_cmpstr(nm_setting_vpn_get_service_type(s_vpn), ==, NMSS_SERVICE_TYPE);
    g_assert_true(nm_setting_vpn_get_persistent(s_vpn));
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "local-user"), ==, g_get_user_name());
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "subnets"), ==, "10.0.0.0/8 172.20.0.0/16");
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "dns"), ==, "none");
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "method"), ==, "nft");
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "fail-closed"), ==, "yes");
    g_assert_null(nm_setting_vpn_get_data_item(s_vpn, "exclude"));
    g_assert_null(nm_setting_vpn_get_data_item(s_vpn, "dns-servers"));
    g_assert_cmpint(nm_setting_ip_config_get_auto_route_ext_gw(
                        nm_connection_get_setting_ip4_config(f->connection)),
                    ==, NM_TERNARY_FALSE);
}

static void
test_dns_rows_follow_the_mode(Fixture *f, gconstpointer user_data)
{
    GtkWidget *mode = find(f->form, "dns");
    GtkWidget *servers = find(f->form, "dns_servers");
    GtkWidget *domains = find(f->form, "dns_domains");
    g_autoptr(GError) error = NULL;

    fill_minimum(f);
    g_assert_false(gtk_widget_get_sensitive(servers));
    g_assert_false(gtk_widget_get_sensitive(domains));

    gtk_drop_down_set_selected(GTK_DROP_DOWN(mode), 1); /* split */
    g_assert_true(gtk_widget_get_sensitive(servers));
    g_assert_true(gtk_widget_get_sensitive(domains));
    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_error(error, NMSS_VALIDATE_ERROR, NMSS_FIELD_DNS_SERVERS);
    g_clear_error(&error);

    type(f, "dns_servers", "10.1.0.53,10.1.0.54");
    type(f, "dns_domains", "corp.example");
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_cmpstr(nm_setting_vpn_get_data_item(nm_connection_get_setting_vpn(f->connection),
                                                 "dns-servers"),
                    ==, "10.1.0.53 10.1.0.54");

    gtk_drop_down_set_selected(GTK_DROP_DOWN(mode), 2); /* all */
    g_assert_false(gtk_widget_get_sensitive(domains));
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_null(nm_setting_vpn_get_data_item(nm_connection_get_setting_vpn(f->connection),
                                               "dns-domains"));
}

static void
test_wrong_rows_are_marked(Fixture *f, gconstpointer user_data)
{
    GtkWidget *subnets = find(f->form, "subnets");
    GtkWidget *remote = find(f->form, "remote");

    /* Nothing is flagged before the user has typed. */
    g_assert_false(gtk_widget_has_css_class(remote, "error"));
    type(f, "subnets", "10.0.0.0/33");
    g_assert_true(gtk_widget_has_css_class(subnets, "error"));
    g_assert_nonnull(gtk_widget_get_tooltip_text(subnets));
    type(f, "subnets", "10.0.0.0/8");
    g_assert_false(gtk_widget_has_css_class(subnets, "error"));

    type(f, "remote", "x");
    type(f, "remote", "");
    g_assert_true(gtk_widget_has_css_class(remote, "error"));
}

static void
test_existing_profile(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;
    GtkWidget *method = find(f->form, "method");
    NMSettingVpn *s_vpn = nm_connection_get_setting_vpn(f->connection);

    /* The form shows the profile as it is, and a method it does not know is
     * refused, as the plugin does, but not replaced. */
    g_assert_cmpstr(gtk_editable_get_text(GTK_EDITABLE(find(f->form, "remote"))), ==, "corp");
    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_error(error, NMSS_VALIDATE_ERROR, NMSS_FIELD_METHOD);
    g_clear_error(&error);
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "method"), ==, "tproxy");

    gtk_drop_down_set_selected(GTK_DROP_DOWN(method), 1); /* nat */
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "method"), ==, "nat");
    g_assert_cmpstr(nm_setting_vpn_get_data_item(s_vpn, "from-a-newer-cli"), ==, "x");
}

static void
test_ipv4_method_is_rechecked(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;
    NMSettingIPConfig *s_ip4;

    fill_minimum(f);
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    f->changed = 0;

    /* The IPv4 page writes after ours, so we meet its new method one
     * validation late, and must ask for another one or the verdict stays. */
    s_ip4 = nm_connection_get_setting_ip4_config(f->connection);
    g_object_set(s_ip4, NM_SETTING_IP_CONFIG_METHOD, NM_SETTING_IP4_CONFIG_METHOD_MANUAL, NULL);
    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_error(error, NMSS_VALIDATE_ERROR, NMSS_FIELD_IPV4_METHOD);
    g_clear_error(&error);
    while (g_main_context_iteration(NULL, FALSE))
        ;
    g_assert_cmpuint(f->changed, ==, 1);

    /* Same method again: nothing to ask for. */
    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_clear_error(&error);
    while (g_main_context_iteration(NULL, FALSE))
        ;
    g_assert_cmpuint(f->changed, ==, 1);

    g_object_set(s_ip4, NM_SETTING_IP_CONFIG_METHOD, NM_SETTING_IP4_CONFIG_METHOD_AUTO, NULL);
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    while (g_main_context_iteration(NULL, FALSE))
        ;
    g_assert_cmpuint(f->changed, ==, 2);
}

static void
test_all_users_disables_apply(Fixture *f, gconstpointer user_data)
{
    g_autoptr(GError) error = NULL;

    fill_minimum(f);
    g_assert_true(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    /* "Make available to other users" clears the permissions. */
    g_object_set(nm_connection_get_setting_connection(f->connection), NM_SETTING_CONNECTION_ID,
                 "corp", NULL);
    nm_setting_connection_remove_permission(nm_connection_get_setting_connection(f->connection),
                                            0);
    g_assert_false(nm_vpn_editor_update_connection(f->editor, f->connection, &error));
    g_assert_error(error, NMSS_VALIDATE_ERROR, NMSS_FIELD_PERMISSIONS);
}

int
main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_assert_cmpint(argc, ==, 2);
    plugin_path = argv[1];
    if (!gtk_init_check()) {
        g_print("1..0 # SKIP no display\n");
        return 77;
    }

    g_test_add("/editor/names-the-service", Fixture, "new", setup, test_names_the_service,
               teardown);
    g_test_add("/editor/new-profile-needs-input", Fixture, "new", setup,
               test_new_profile_needs_input, teardown);
    g_test_add("/editor/new-profile-is-written", Fixture, "new", setup,
               test_new_profile_is_written, teardown);
    g_test_add("/editor/dns-rows-follow-the-mode", Fixture, "new", setup,
               test_dns_rows_follow_the_mode, teardown);
    g_test_add("/editor/wrong-rows-are-marked", Fixture, "new", setup,
               test_wrong_rows_are_marked, teardown);
    g_test_add("/editor/existing-profile", Fixture, "cli", setup, test_existing_profile,
               teardown);
    g_test_add("/editor/ipv4-method-is-rechecked", Fixture, "new", setup,
               test_ipv4_method_is_rechecked, teardown);
    g_test_add("/editor/all-users-disables-apply", Fixture, "new", setup,
               test_all_users_disables_apply, teardown);
    return g_test_run();
}
