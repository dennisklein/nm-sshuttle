/* SPDX-License-Identifier: MIT */
/*
 * The base plugin, libnm-vpn-plugin-sshuttle.so: it names the VPN and hands
 * out the editor, which lives in a second library per toolkit. Only this
 * library depends on libnm alone, so that a GTK3 process (nm-connection-editor)
 * can load it without pulling GTK4 in.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <gmodule.h>

#include <NetworkManager.h>

#include "nm-sshuttle-i18n.h"

#define PLUGIN_NAME "sshuttle"
#define SERVICE_TYPE "org.freedesktop.NetworkManager.sshuttle"
#define EDITOR_LIBRARY "libnm-gtk4-vpn-plugin-sshuttle-editor.so"
#define EDITOR_FACTORY "nm_vpn_editor_factory_sshuttle"

typedef NMVpnEditor *(*EditorFactory)(NMVpnEditorPlugin *, NMConnection *, GError **);

enum { PROP_0, PROP_NAME, PROP_DESC, PROP_SERVICE };

#define NMSS_TYPE_EDITOR_PLUGIN (nmss_editor_plugin_get_type())
G_DECLARE_FINAL_TYPE(NmssEditorPlugin, nmss_editor_plugin, NMSS, EDITOR_PLUGIN, GObject)

struct _NmssEditorPlugin {
    GObject parent;
};

static void nmss_editor_plugin_interface_init(NMVpnEditorPluginInterface *iface);

G_DEFINE_TYPE_WITH_CODE(NmssEditorPlugin, nmss_editor_plugin, G_TYPE_OBJECT,
                        G_IMPLEMENT_INTERFACE(NM_TYPE_VPN_EDITOR_PLUGIN,
                                              nmss_editor_plugin_interface_init))

/* GTK4 has no gtk_container_add; a process that has it is GTK3. */
static gboolean
process_is_gtk3(void)
{
    GModule *self = g_module_open(NULL, 0);
    gpointer symbol = NULL;

    if (self) {
        g_module_symbol(self, "gtk_container_add", &symbol);
        g_module_close(self);
    }
    return symbol != NULL;
}

static EditorFactory
load_factory(GError **error)
{
    static EditorFactory factory;
    g_autofree char *dir = NULL;
    g_autofree char *path = NULL;
    Dl_info info;
    GModule *module;

    if (factory)
        return factory;
    if (process_is_gtk3()) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                            _("nm-sshuttle has no GTK3 editor"));
        return NULL;
    }
    /* The editor sits next to this library. */
    if (!dladdr((void *) load_factory, &info) || !info.dli_fname) {
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_FAILED,
                            _("cannot find the directory of the sshuttle plugin"));
        return NULL;
    }
    dir = g_path_get_dirname(info.dli_fname);
    path = g_build_filename(dir, EDITOR_LIBRARY, NULL);
    module = g_module_open(path, G_MODULE_BIND_LAZY | G_MODULE_BIND_LOCAL);
    if (!module) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, _("cannot load the editor “%s”: %s"),
                    path, g_module_error());
        return NULL;
    }
    if (!g_module_symbol(module, EDITOR_FACTORY, (gpointer *) &factory)) {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, _("cannot find %s in “%s”: %s"),
                    EDITOR_FACTORY, path, g_module_error());
        g_module_close(module);
        factory = NULL;
        return NULL;
    }
    /* The editor registers GTypes, which cannot be unregistered. */
    g_module_make_resident(module);
    return factory;
}

static NMVpnEditor *
get_editor(NMVpnEditorPlugin *plugin, NMConnection *connection, GError **error)
{
    EditorFactory factory;

    g_return_val_if_fail(NM_IS_CONNECTION(connection), NULL);
    g_return_val_if_fail(!error || !*error, NULL);

    factory = load_factory(error);
    return factory ? factory(plugin, connection, error) : NULL;
}

static NMVpnEditorPluginCapability
get_capabilities(NMVpnEditorPlugin *plugin)
{
    return NM_VPN_EDITOR_PLUGIN_CAPABILITY_NONE;
}

static void
get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
    switch (prop_id) {
    case PROP_NAME:
        g_value_set_string(value, PLUGIN_NAME);
        break;
    case PROP_DESC:
        g_value_set_string(value, _("Tunnels networks over ssh with sshuttle."));
        break;
    case PROP_SERVICE:
        g_value_set_string(value, SERVICE_TYPE);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
        break;
    }
}

static void
nmss_editor_plugin_init(NmssEditorPlugin *plugin)
{
}

static void
nmss_editor_plugin_interface_init(NMVpnEditorPluginInterface *iface)
{
    iface->get_editor = get_editor;
    iface->get_capabilities = get_capabilities;
}

static void
nmss_editor_plugin_class_init(NmssEditorPluginClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);

    object_class->get_property = get_property;
    g_object_class_override_property(object_class, PROP_NAME, NM_VPN_EDITOR_PLUGIN_NAME);
    g_object_class_override_property(object_class, PROP_DESC, NM_VPN_EDITOR_PLUGIN_DESCRIPTION);
    g_object_class_override_property(object_class, PROP_SERVICE, NM_VPN_EDITOR_PLUGIN_SERVICE);
}

G_MODULE_EXPORT NMVpnEditorPlugin *
nm_vpn_editor_plugin_factory(GError **error)
{
    g_return_val_if_fail(!error || !*error, NULL);

    bindtextdomain(GETTEXT_PACKAGE, LOCALEDIR);
    bind_textdomain_codeset(GETTEXT_PACKAGE, "UTF-8");
    return g_object_new(NMSS_TYPE_EDITOR_PLUGIN, NULL);
}
