/* SPDX-License-Identifier: MIT */
/*
 * Runs every case of tests/profile-cases.json through nmss_profile_validate().
 * tests/test_profile_cases.py runs the same file through profile.parse.
 */

#include <json-glib/json-glib.h>
#include <string.h>

#include "nm-sshuttle-validate.h"

static JsonObject *base;

static void
run_case(gconstpointer user_data)
{
    JsonObject *c = (JsonObject *) user_data;
    g_autoptr(GHashTable) data = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
    g_autoptr(GError) error = NULL;
    g_autofree const char **permissions = NULL;
    JsonArray *perms = json_object_get_array_member(base, "permissions");
    const char *ipv4_method = json_object_get_string_member(base, "ipv4_method");
    gboolean persistent = json_object_get_boolean_member(base, "persistent");
    JsonObject *overrides;
    NmssProfile profile;
    const char *expected = json_object_get_string_member_with_default(c, "error", NULL);
    gboolean ok;

    if (json_object_get_boolean_member_with_default(c, "plugin_only", FALSE)) {
        g_test_skip("needs the user database, which only the plugin reads");
        return;
    }

    for (guint i = 0; i < 2; i++) {
        JsonObject *source = i == 0 ? json_object_get_object_member(base, "data")
                             : json_object_has_member(c, "data")
                                 ? json_object_get_object_member(c, "data")
                                 : NULL;
        g_autoptr(GList) members = source ? json_object_get_members(source) : NULL;

        for (GList *m = members; m; m = m->next) {
            JsonNode *node = json_object_get_member(source, m->data);

            if (JSON_NODE_HOLDS_NULL(node))
                g_hash_table_remove(data, m->data);
            else
                g_hash_table_replace(data, g_strdup(m->data), g_strdup(json_node_get_string(node)));
        }
    }

    if (json_object_has_member(c, "permissions"))
        perms = json_object_get_array_member(c, "permissions");
    permissions = g_new0(const char *, json_array_get_length(perms) + 1);
    for (guint i = 0; i < json_array_get_length(perms); i++)
        permissions[i] = json_array_get_string_element(perms, i);

    overrides = c;
    if (json_object_has_member(overrides, "persistent"))
        persistent = json_object_get_boolean_member(overrides, "persistent");
    if (json_object_has_member(overrides, "ipv4_method"))
        ipv4_method = json_object_get_string_member_with_default(overrides, "ipv4_method", NULL);

    profile.data = data;
    profile.permissions = (const char *const *) permissions;
    profile.persistent = persistent;
    profile.ipv4_method = ipv4_method;

    ok = nmss_profile_validate(&profile, &error);
    if (!expected) {
        g_assert_no_error(error);
        g_assert_true(ok);
    } else {
        g_assert_false(ok);
        g_assert_nonnull(error);
        g_assert_true(error->domain == NMSS_VALIDATE_ERROR);
        g_assert_cmpstr(nmss_field_key(error->code), ==, expected);
    }
}

static char *
test_path(const char *name)
{
    char *path = g_strconcat("/validate/", name, NULL);

    for (char *p = path + strlen("/validate/"); *p; p++) {
        if (!g_ascii_isalnum(*p))
            *p = '-';
    }
    return path;
}

int
main(int argc, char **argv)
{
    g_autoptr(JsonParser) parser = json_parser_new();
    g_autoptr(GError) error = NULL;
    JsonObject *root;
    JsonArray *cases;

    g_test_init(&argc, &argv, NULL);
    g_assert_cmpint(argc, ==, 2);
    json_parser_load_from_file(parser, argv[1], &error);
    g_assert_no_error(error);

    root = json_node_get_object(json_parser_get_root(parser));
    base = json_object_get_object_member(root, "base");
    cases = json_object_get_array_member(root, "cases");
    for (guint i = 0; i < json_array_get_length(cases); i++) {
        JsonObject *c = json_array_get_object_element(cases, i);
        g_autofree char *path = test_path(json_object_get_string_member(c, "name"));

        g_test_add_data_func(path, c, run_case);
    }
    return g_test_run();
}
