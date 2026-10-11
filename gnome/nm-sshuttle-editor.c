/* SPDX-License-Identifier: MIT */
/*
 * The editor widget (GTK4) behind libnm-gtk4-vpn-plugin-sshuttle-editor.so.
 * What is checked and written is in nm-sshuttle-validate.c and
 * nm-sshuttle-connection.c; this file moves values between those and the form.
 */

#include "nm-sshuttle-editor.h"

#include <gtk/gtk.h>

#include "nm-sshuttle-connection.h"
#include "nm-sshuttle-i18n.h"
#include "nm-sshuttle-validate.h"

#define RESOURCE "/io/github/dennisklein/nm-sshuttle/nm-sshuttle-editor.ui"

static const char *const DNS_MODES[] = {"none", "split", "all"};
static const char *const METHODS[] = {"nft", "nat"};

/* The rows of the form, in the order of NmssEditor.rows. */
enum {
    ROW_REMOTE,
    ROW_SUBNETS,
    ROW_EXCLUDE,
    ROW_DNS,
    ROW_DNS_SERVERS,
    ROW_DNS_DOMAINS,
    ROW_GATEWAY,
    ROW_PROBE,
    ROW_METHOD,
    ROW_FAIL_CLOSED,
    N_ROWS
};

static const struct {
    NmssField field;
    const char *id;
} ROWS[N_ROWS] = {
    [ROW_REMOTE] = {NMSS_FIELD_REMOTE, "remote"},
    [ROW_SUBNETS] = {NMSS_FIELD_SUBNETS, "subnets"},
    [ROW_EXCLUDE] = {NMSS_FIELD_EXCLUDE, "exclude"},
    [ROW_DNS] = {NMSS_FIELD_DNS, "dns"},
    [ROW_DNS_SERVERS] = {NMSS_FIELD_DNS_SERVERS, "dns_servers"},
    [ROW_DNS_DOMAINS] = {NMSS_FIELD_DNS_DOMAINS, "dns_domains"},
    [ROW_GATEWAY] = {NMSS_FIELD_GATEWAY, "gateway"},
    [ROW_PROBE] = {NMSS_FIELD_PROBE, "probe"},
    [ROW_METHOD] = {NMSS_FIELD_METHOD, "method"},
    [ROW_FAIL_CLOSED] = {NMSS_FIELD_FAIL_CLOSED, "fail_closed"},
};

typedef struct _NmssEditor NmssEditor;

typedef struct {
    NmssEditor *editor;
    GtkWidget *widget;
    gboolean touched; /* the user changed it, so that "empty" can be an error */
} Row;

struct _NmssEditor {
    GObject parent;
    GtkWidget *form;
    GHashTable *data; /* vpn.data as it was read: keys of other versions stay in it */
    char *local_user;
    char *seen_ipv4_method; /* what the IPv4 page had written when we last validated */
    guint recheck_id;
    Row rows[N_ROWS];
};

typedef struct {
    GObjectClass parent_class;
} NmssEditorClass;

#define NMSS_TYPE_EDITOR (nmss_editor_get_type())
GType nmss_editor_get_type(void);

static void nmss_editor_interface_init(NMVpnEditorInterface *iface);

G_DEFINE_TYPE_WITH_CODE(NmssEditor, nmss_editor, G_TYPE_OBJECT,
                        G_IMPLEMENT_INTERFACE(NM_TYPE_VPN_EDITOR, nmss_editor_interface_init))

/*****************************************************************************/

/*
 * A drop-down shows the value of the profile even when this version does not
 * know it, so that opening and applying does not change it silently. The
 * check then refuses it, as the plugin would.
 */
static void
dropdown_select(GtkWidget *dropdown, const char *const *known, guint n_known, const char *value)
{
    GtkStringList *model = GTK_STRING_LIST(gtk_drop_down_get_model(GTK_DROP_DOWN(dropdown)));
    guint selected = 0;
    gboolean found = FALSE;

    for (guint i = 0; value && i < n_known; i++) {
        if (g_str_equal(known[i], value)) {
            selected = i;
            found = TRUE;
        }
    }
    if (value && *value && !found) {
        gtk_string_list_append(model, value);
        selected = n_known;
    }
    gtk_drop_down_set_selected(GTK_DROP_DOWN(dropdown), selected);
}

static char *
dropdown_value(GtkWidget *dropdown, const char *const *known, guint n_known)
{
    guint selected = gtk_drop_down_get_selected(GTK_DROP_DOWN(dropdown));
    GtkStringList *model = GTK_STRING_LIST(gtk_drop_down_get_model(GTK_DROP_DOWN(dropdown)));

    if (selected < n_known)
        return g_strdup(known[selected]);
    return g_strdup(gtk_string_list_get_string(model, selected));
}

static void
set_text(NmssEditor *self, guint row, const char *key)
{
    const char *value = g_hash_table_lookup(self->data, key);

    gtk_editable_set_text(GTK_EDITABLE(self->rows[row].widget), value ? value : "");
}

/* Keeps @value in @data, or removes the key when it is empty. Takes @value. */
static void
set_value(GHashTable *data, const char *key, char *value)
{
    if (value && *value)
        g_hash_table_replace(data, g_strdup(key), value);
    else {
        g_hash_table_remove(data, key);
        g_free(value);
    }
}

static char *
entry_text(NmssEditor *self, guint row)
{
    return g_strstrip(g_strdup(gtk_editable_get_text(GTK_EDITABLE(self->rows[row].widget))));
}

static char *
entry_list(NmssEditor *self, guint row)
{
    return nmss_normalize_list(gtk_editable_get_text(GTK_EDITABLE(self->rows[row].widget)));
}

static char *
selected_dns(NmssEditor *self)
{
    return dropdown_value(self->rows[ROW_DNS].widget, DNS_MODES, G_N_ELEMENTS(DNS_MODES));
}

/* The vpn.data that the form stands for. */
static GHashTable *
collect(NmssEditor *self)
{
    GHashTable *data = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
    g_autofree char *dns = selected_dns(self);
    GHashTableIter iter;
    gpointer key, value;

    g_hash_table_iter_init(&iter, self->data);
    while (g_hash_table_iter_next(&iter, &key, &value))
        g_hash_table_replace(data, g_strdup(key), g_strdup(value));

    set_value(data, "remote", entry_text(self, ROW_REMOTE));
    set_value(data, "local-user", g_strdup(self->local_user));
    set_value(data, "subnets", entry_list(self, ROW_SUBNETS));
    set_value(data, "exclude", entry_list(self, ROW_EXCLUDE));
    set_value(data, "dns", g_strdup(dns));
    /* What the mode does not use is left out: the plugin ignores it. */
    set_value(data, "dns-servers",
              g_str_equal(dns, "none") ? NULL : entry_list(self, ROW_DNS_SERVERS));
    set_value(data, "dns-domains",
              g_str_equal(dns, "split") ? entry_list(self, ROW_DNS_DOMAINS) : NULL);
    set_value(data, "gateway", entry_text(self, ROW_GATEWAY));
    set_value(data, "probe", entry_text(self, ROW_PROBE));
    set_value(data, "method",
              dropdown_value(self->rows[ROW_METHOD].widget, METHODS, G_N_ELEMENTS(METHODS)));
    set_value(data, "fail-closed",
              g_strdup(gtk_switch_get_active(GTK_SWITCH(self->rows[ROW_FAIL_CLOSED].widget))
                           ? "yes"
                           : "no"));
    return data;
}

static gboolean
check_row(guint row, GHashTable *data, GError **error)
{
    const char *value = g_hash_table_lookup(data, nmss_field_key(ROWS[row].field));
    const char *dns = g_hash_table_lookup(data, "dns");

    switch (ROWS[row].field) {
    case NMSS_FIELD_REMOTE:
        return nmss_check_remote(value, error);
    case NMSS_FIELD_SUBNETS:
        return nmss_check_subnets(value, error);
    case NMSS_FIELD_EXCLUDE:
        return nmss_check_exclude(value, error);
    case NMSS_FIELD_DNS:
        return nmss_check_dns(value, error);
    case NMSS_FIELD_DNS_SERVERS:
        return nmss_check_dns_servers(dns, value, error);
    case NMSS_FIELD_DNS_DOMAINS:
        return nmss_check_dns_domains(dns, value, error);
    case NMSS_FIELD_GATEWAY:
        return nmss_check_gateway(value, error);
    case NMSS_FIELD_PROBE:
        return nmss_check_probe(value, error);
    case NMSS_FIELD_METHOD:
        return nmss_check_method(value, error);
    case NMSS_FIELD_FAIL_CLOSED:
        return nmss_check_fail_closed(value, error);
    default:
        return TRUE;
    }
}

/* Marks the rows that are wrong. An empty row counts as wrong once touched. */
static void
mark_rows(NmssEditor *self)
{
    g_autoptr(GHashTable) data = collect(self);

    for (guint i = 0; i < N_ROWS; i++) {
        Row *row = &self->rows[i];
        g_autoptr(GError) error = NULL;
        const char *value = g_hash_table_lookup(data, nmss_field_key(ROWS[i].field));

        if (!check_row(i, data, &error) && (row->touched || (value && *value))) {
            gtk_widget_add_css_class(row->widget, "error");
            gtk_widget_set_tooltip_text(row->widget, error->message);
        } else {
            gtk_widget_remove_css_class(row->widget, "error");
            gtk_widget_set_tooltip_text(row->widget, NULL);
        }
    }
}

static void
update_sensitivity(NmssEditor *self)
{
    g_autofree char *dns = selected_dns(self);

    gtk_widget_set_sensitive(self->rows[ROW_DNS_SERVERS].widget, !g_str_equal(dns, "none"));
    gtk_widget_set_sensitive(self->rows[ROW_DNS_DOMAINS].widget, g_str_equal(dns, "split"));
}

static void
row_changed(Row *row)
{
    NmssEditor *self = row->editor;

    row->touched = TRUE;
    update_sensitivity(self);
    mark_rows(self);
    g_signal_emit_by_name(self, "changed");
}

static void
on_entry_changed(GtkEditable *editable, gpointer row)
{
    row_changed(row);
}

static void
on_property_changed(GObject *object, GParamSpec *pspec, gpointer row)
{
    row_changed(row);
}

/*****************************************************************************/

static void
populate(NmssEditor *self)
{
    const char *fail_closed = g_hash_table_lookup(self->data, "fail-closed");

    set_text(self, ROW_REMOTE, "remote");
    set_text(self, ROW_SUBNETS, "subnets");
    set_text(self, ROW_EXCLUDE, "exclude");
    set_text(self, ROW_DNS_SERVERS, "dns-servers");
    set_text(self, ROW_DNS_DOMAINS, "dns-domains");
    set_text(self, ROW_GATEWAY, "gateway");
    set_text(self, ROW_PROBE, "probe");
    dropdown_select(self->rows[ROW_DNS].widget, DNS_MODES, G_N_ELEMENTS(DNS_MODES),
                    g_hash_table_lookup(self->data, "dns"));
    dropdown_select(self->rows[ROW_METHOD].widget, METHODS, G_N_ELEMENTS(METHODS),
                    g_hash_table_lookup(self->data, "method"));
    gtk_switch_set_active(GTK_SWITCH(self->rows[ROW_FAIL_CLOSED].widget),
                          !fail_closed || !g_strv_contains((const char *const[]) {"no", "false", "0", NULL},
                                                           fail_closed));
    update_sensitivity(self);
    mark_rows(self);
}

static gboolean
build(NmssEditor *self, NMConnection *connection, GError **error)
{
    g_autoptr(GtkBuilder) builder = gtk_builder_new();
    const char *local_user;
    GtkWidget *label;

    gtk_builder_set_translation_domain(builder, GETTEXT_PACKAGE);
    if (!gtk_builder_add_from_resource(builder, RESOURCE, error))
        return FALSE;
    self->form = g_object_ref(GTK_WIDGET(gtk_builder_get_object(builder, "form")));
    for (guint i = 0; i < N_ROWS; i++) {
        self->rows[i].editor = self;
        self->rows[i].widget = GTK_WIDGET(gtk_builder_get_object(builder, ROWS[i].id));
        g_return_val_if_fail(self->rows[i].widget, FALSE);
        gtk_widget_set_name(self->rows[i].widget, ROWS[i].id);
    }

    /* A new profile belongs to the user who makes it; Settings has given it
     * the permission already. An existing one keeps its user. */
    self->data = nmss_connection_get_data(connection);
    local_user = g_hash_table_lookup(self->data, "local-user");
    self->local_user = g_strdup(local_user && *local_user ? local_user : g_get_user_name());
    label = GTK_WIDGET(gtk_builder_get_object(builder, "local_user"));
    gtk_label_set_text(GTK_LABEL(label), self->local_user);

    populate(self);

    /* Only now, so that filling the form is not taken for the user's changes. */
    for (guint i = 0; i < N_ROWS; i++) {
        GtkWidget *w = self->rows[i].widget;

        if (GTK_IS_EDITABLE(w))
            g_signal_connect(w, "changed", G_CALLBACK(on_entry_changed), &self->rows[i]);
        else if (GTK_IS_DROP_DOWN(w))
            g_signal_connect(w, "notify::selected", G_CALLBACK(on_property_changed), &self->rows[i]);
        else
            g_signal_connect(w, "notify::active", G_CALLBACK(on_property_changed), &self->rows[i]);
    }
    return TRUE;
}

static GObject *
get_widget(NMVpnEditor *editor)
{
    return G_OBJECT(((NmssEditor *) editor)->form);
}

static gboolean
recheck(gpointer user_data)
{
    NmssEditor *self = user_data;

    self->recheck_id = 0;
    g_signal_emit_by_name(self, "changed");
    return G_SOURCE_REMOVE;
}

/*
 * Settings validates the tabs in order and the IPv4 tab writes after ours, so
 * we judge ipv4.method as the IPv4 tab left it one validation ago. When that
 * differs from what we saw last time, ask for one more validation, so a
 * verdict based on the old value does not stay on screen.
 */
static void
watch_ipv4_method(NmssEditor *self, NMConnection *connection)
{
    NMSettingIPConfig *s_ip4 = nm_connection_get_setting_ip4_config(connection);
    const char *method = s_ip4 ? nm_setting_ip_config_get_method(s_ip4) : NULL;
    gboolean first = self->seen_ipv4_method == NULL;

    if (g_strcmp0(self->seen_ipv4_method, method ? method : "") == 0)
        return;
    g_free(self->seen_ipv4_method);
    self->seen_ipv4_method = g_strdup(method ? method : "");
    if (!first && !self->recheck_id)
        self->recheck_id = g_idle_add(recheck, self);
}

static gboolean
update_connection(NMVpnEditor *editor, NMConnection *connection, GError **error)
{
    NmssEditor *self = (NmssEditor *) editor;
    g_autoptr(GHashTable) data = collect(self);

    watch_ipv4_method(self, connection);
    return nmss_connection_update(connection, data, error);
}

static void
nmss_editor_interface_init(NMVpnEditorInterface *iface)
{
    iface->get_widget = get_widget;
    iface->update_connection = update_connection;
}

static void
nmss_editor_dispose(GObject *object)
{
    NmssEditor *self = (NmssEditor *) object;

    g_clear_handle_id(&self->recheck_id, g_source_remove);
    g_clear_object(&self->form);
    G_OBJECT_CLASS(nmss_editor_parent_class)->dispose(object);
}

static void
nmss_editor_finalize(GObject *object)
{
    NmssEditor *self = (NmssEditor *) object;

    g_clear_pointer(&self->data, g_hash_table_unref);
    g_free(self->local_user);
    g_free(self->seen_ipv4_method);
    G_OBJECT_CLASS(nmss_editor_parent_class)->finalize(object);
}

static void
nmss_editor_init(NmssEditor *self)
{
}

static void
nmss_editor_class_init(NmssEditorClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);

    object_class->dispose = nmss_editor_dispose;
    object_class->finalize = nmss_editor_finalize;
}

G_MODULE_EXPORT NMVpnEditor *
nm_vpn_editor_factory_sshuttle(NMVpnEditorPlugin *editor_plugin, NMConnection *connection,
                               GError **error)
{
    NmssEditor *self;

    g_return_val_if_fail(!error || !*error, NULL);

    self = g_object_new(NMSS_TYPE_EDITOR, NULL);
    if (!build(self, connection, error)) {
        g_object_unref(self);
        return NULL;
    }
    return NM_VPN_EDITOR(self);
}
