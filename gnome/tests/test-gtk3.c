/* SPDX-License-Identifier: MIT */
/*
 * The base plugin must not load the GTK4 editor into a GTK3 process
 * (nm-connection-editor): it would abort it. A GTK3 process is one that has
 * gtk_container_add, which this program defines. No display needed.
 *
 *   test-gtk3 PLUGIN.so
 */

#include <NetworkManager.h>

#include "nm-sshuttle-connection.h"

G_MODULE_EXPORT void
gtk_container_add(void *container, void *widget)
{
}

static const char *plugin_path;

static void
test_refuses_gtk3(void)
{
    g_autoptr(NMVpnEditorPlugin) plugin = NULL;
    g_autoptr(NMConnection) connection = nm_simple_connection_new();
    g_autoptr(GError) error = NULL;

    plugin = nm_vpn_editor_plugin_load_from_file(plugin_path, NMSS_SERVICE_TYPE, -1, NULL, NULL,
                                                 &error);
    g_assert_no_error(error);
    g_assert_null(nm_vpn_editor_plugin_get_editor(plugin, connection, &error));
    g_assert_nonnull(error);
    g_assert_nonnull(strstr(error->message, "no GTK3 editor"));
}

int
main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_assert_cmpint(argc, ==, 2);
    plugin_path = argv[1];
    g_test_add_func("/gtk3/refuses", test_refuses_gtk3);
    return g_test_run();
}
