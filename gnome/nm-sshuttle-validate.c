/* SPDX-License-Identifier: MIT */

#include "nm-sshuttle-validate.h"

#include <arpa/inet.h>
#include <string.h>

#include "nm-sshuttle-i18n.h"

G_DEFINE_QUARK(nmss-validate-error-quark, nmss_validate_error)

const char *
nmss_field_key(NmssField field)
{
    switch (field) {
    case NMSS_FIELD_REMOTE:
        return "remote";
    case NMSS_FIELD_LOCAL_USER:
        return "local-user";
    case NMSS_FIELD_PERMISSIONS:
        return "connection.permissions";
    case NMSS_FIELD_PERSISTENT:
        return "vpn.persistent";
    case NMSS_FIELD_IPV4_METHOD:
        return "ipv4.method";
    case NMSS_FIELD_SUBNETS:
        return "subnets";
    case NMSS_FIELD_EXCLUDE:
        return "exclude";
    case NMSS_FIELD_DNS:
        return "dns";
    case NMSS_FIELD_DNS_SERVERS:
        return "dns-servers";
    case NMSS_FIELD_DNS_DOMAINS:
        return "dns-domains";
    case NMSS_FIELD_METHOD:
        return "method";
    case NMSS_FIELD_GATEWAY:
        return "gateway";
    case NMSS_FIELD_PROBE:
        return "probe";
    case NMSS_FIELD_FAIL_CLOSED:
        return "fail-closed";
    }
    g_return_val_if_reached("");
}

G_GNUC_PRINTF(3, 4)
static gboolean
fail(GError **error, NmssField field, const char *format, ...)
{
    va_list args;
    g_autofree char *message = NULL;

    va_start(args, format);
    message = g_strdup_vprintf(format, args);
    va_end(args);
    g_set_error_literal(error, NMSS_VALIDATE_ERROR, field, message);
    return FALSE;
}

static gboolean
is_blank(const char *s)
{
    return !s || !*s;
}

/* Words are separated by commas and white space, as in profile.py's _split(). */
static char **
split(const char *value)
{
    GPtrArray *words = g_ptr_array_new();
    const char *p = value ? value : "";

    while (*p) {
        const char *start;

        while (*p && (*p == ',' || g_ascii_isspace(*p)))
            p++;
        start = p;
        while (*p && *p != ',' && !g_ascii_isspace(*p))
            p++;
        if (p > start)
            g_ptr_array_add(words, g_strndup(start, p - start));
    }
    g_ptr_array_add(words, NULL);
    return (char **) g_ptr_array_free(words, FALSE);
}

char *
nmss_normalize_list(const char *value)
{
    g_auto(GStrv) words = split(value);

    return g_strjoinv(" ", words);
}

static gboolean
parse_ipv4(const char *s, guint32 *out)
{
    struct in_addr addr;

    if (inet_pton(AF_INET, s, &addr) != 1)
        return FALSE;
    if (out)
        *out = g_ntohl(addr.s_addr);
    return TRUE;
}

static gboolean
parse_ipv6(const char *s)
{
    struct in6_addr addr;

    return inet_pton(AF_INET6, s, &addr) == 1;
}

static gboolean
all_digits(const char *s)
{
    if (!*s)
        return FALSE;
    for (; *s; s++) {
        if (!g_ascii_isdigit(*s))
            return FALSE;
    }
    return TRUE;
}

/* A prefix length, a netmask or a hostmask, as Python's ipaddress takes them. */
static gboolean
valid_ipv4_mask(const char *s)
{
    guint32 mask;

    if (all_digits(s)) {
        gsize len = strlen(s);

        return len <= 3 && g_ascii_strtoull(s, NULL, 10) <= 32;
    }
    if (!parse_ipv4(s, &mask))
        return FALSE;
    return (~mask & (~mask + 1)) == 0 || (mask & (mask + 1)) == 0;
}

static gboolean
check_network(NmssField field, const char *item, GError **error)
{
    g_autofree char *addr = NULL;
    const char *slash = strchr(item, '/');

    addr = slash ? g_strndup(item, slash - item) : g_strdup(item);
    if (parse_ipv4(addr, NULL) && (!slash || valid_ipv4_mask(slash + 1)))
        return TRUE;
    if (parse_ipv6(addr)
        && (!slash || (all_digits(slash + 1) && strlen(slash + 1) <= 3
                       && g_ascii_strtoull(slash + 1, NULL, 10) <= 128)))
        return TRUE;
    return fail(error, field, _("%s: “%s” is not a network, such as 10.0.0.0/8 or 2001:db8::/32"),
                field == NMSS_FIELD_SUBNETS ? _("Networks") : _("Exclude"), item);
}

static gboolean
check_networks(NmssField field, const char *value, gboolean required, GError **error)
{
    g_auto(GStrv) items = split(value);

    if (required && !*items)
        return fail(error, field, _("Networks: at least one network is required"));
    for (char **item = items; *item; item++) {
        if (!check_network(field, *item, error))
            return FALSE;
    }
    return TRUE;
}

gboolean
nmss_check_remote(const char *value, GError **error)
{
    if (is_blank(value))
        return fail(error, NMSS_FIELD_REMOTE, _("SSH destination is required"));
    if (*value == '-' || strspn(value, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                       "0123456789._@:%[]-") != strlen(value))
        return fail(error, NMSS_FIELD_REMOTE,
                    _("SSH destination: “%s” is not valid. Use a host name, an alias from "
                      "~/.ssh/config or user@host, and do not start with “-”"),
                    value);
    return TRUE;
}

gboolean
nmss_check_local_user(const char *value, GError **error)
{
    gboolean ok = value && (g_ascii_isalpha(*value) || *value == '_');

    for (const char *p = value; ok && *p; p++)
        ok = g_ascii_isalnum(*p) || *p == '_' || *p == '.' || *p == '-';
    if (!ok)
        return fail(error, NMSS_FIELD_LOCAL_USER, _("Local user “%s” is not a plain user name"),
                    value ? value : "");
    return TRUE;
}

gboolean
nmss_check_subnets(const char *value, GError **error)
{
    return check_networks(NMSS_FIELD_SUBNETS, value, TRUE, error);
}

gboolean
nmss_check_exclude(const char *value, GError **error)
{
    return check_networks(NMSS_FIELD_EXCLUDE, value, FALSE, error);
}

gboolean
nmss_check_dns(const char *value, GError **error)
{
    if (value && !g_strv_contains((const char *const[]) {"none", "split", "all", NULL}, value))
        return fail(error, NMSS_FIELD_DNS, _("DNS: expected none, split or all, not “%s”"), value);
    return TRUE;
}

gboolean
nmss_check_dns_servers(const char *dns, const char *value, GError **error)
{
    g_auto(GStrv) servers = split(value);

    for (char **server = servers; *server; server++) {
        if (!parse_ipv4(*server, NULL))
            return fail(error, NMSS_FIELD_DNS_SERVERS,
                        _("DNS servers: “%s” is not an IPv4 address"), *server);
    }
    if (g_strcmp0(dns ? dns : "none", "none") != 0 && !*servers)
        return fail(error, NMSS_FIELD_DNS_SERVERS, _("DNS servers: at least one is required"));
    return TRUE;
}

/* [~]( "." | label ( "." label )* [.] ), a label being letters, digits and "-". */
static gboolean
is_domain(const char *s)
{
    gsize len;
    gboolean in_label = FALSE;

    if (*s == '~')
        s++;
    if (g_str_equal(s, "."))
        return TRUE;
    len = strlen(s);
    if (len && s[len - 1] == '.')
        len--;
    for (gsize i = 0; i < len; i++) {
        if (s[i] == '.') {
            if (!in_label)
                return FALSE;
            in_label = FALSE;
        } else if (g_ascii_isalnum(s[i]) || s[i] == '-') {
            in_label = TRUE;
        } else {
            return FALSE;
        }
    }
    return in_label;
}

gboolean
nmss_check_dns_domains(const char *dns, const char *value, GError **error)
{
    g_auto(GStrv) domains = split(value);

    for (char **domain = domains; *domain; domain++) {
        if (!is_domain(*domain))
            return fail(error, NMSS_FIELD_DNS_DOMAINS, _("DNS domains: “%s” is not a domain"),
                        *domain);
    }
    if (g_strcmp0(dns, "split") == 0 && !*domains)
        return fail(error, NMSS_FIELD_DNS_DOMAINS,
                    _("DNS domains: at least one is required to split DNS"));
    return TRUE;
}

gboolean
nmss_check_method(const char *value, GError **error)
{
    if (value && !g_strv_contains((const char *const[]) {"nft", "nat", NULL}, value))
        return fail(error, NMSS_FIELD_METHOD, _("Method: expected nft or nat, not “%s”"), value);
    return TRUE;
}

gboolean
nmss_check_gateway(const char *value, GError **error)
{
    if (!is_blank(value) && !parse_ipv4(value, NULL) && !parse_ipv6(value))
        return fail(error, NMSS_FIELD_GATEWAY, _("First hop: “%s” is not an IP address"), value);
    return TRUE;
}

gboolean
nmss_check_probe(const char *value, GError **error)
{
    const char *colon;
    g_autofree char *host = NULL;
    gsize len;
    guint64 port = 0;
    gboolean ok = FALSE;

    if (is_blank(value))
        return TRUE;
    colon = strrchr(value, ':');
    if (colon && all_digits(colon + 1)) {
        for (const char *p = colon + 1; *p && port <= 65535; p++)
            port = port * 10 + (*p - '0');
        host = g_strndup(value, colon - value);
    }
    if (host && port > 0 && port <= 65535) {
        len = strlen(host);
        if (len >= 2 && host[0] == '[' && host[len - 1] == ']') {
            g_autofree char *inner = g_strndup(host + 1, len - 2);

            ok = parse_ipv6(inner);
        } else {
            ok = parse_ipv4(host, NULL);
        }
    }
    if (!ok)
        return fail(error, NMSS_FIELD_PROBE,
                    _("Probe: expected ADDRESS:PORT or [IPV6]:PORT, not “%s”"), value);
    return TRUE;
}

gboolean
nmss_check_fail_closed(const char *value, GError **error)
{
    if (!is_blank(value)
        && !g_strv_contains((const char *const[]) {"yes", "true", "1", "no", "false", "0", NULL},
                            value))
        return fail(error, NMSS_FIELD_FAIL_CLOSED, _("Block traffic while down: expected yes or no"));
    return TRUE;
}

static const char *
data_get(const NmssProfile *profile, const char *key)
{
    return profile->data ? g_hash_table_lookup(profile->data, key) : NULL;
}

gboolean
nmss_profile_validate(const NmssProfile *profile, GError **error)
{
    const char *user = data_get(profile, "local-user");
    g_autofree char *expected = NULL;
    gsize count = 0;
    gboolean private_to_user = FALSE;
    const char *dns = data_get(profile, "dns");

    if (!nmss_check_remote(data_get(profile, "remote"), error)
        || !nmss_check_local_user(user, error))
        return FALSE;

    expected = g_strdup_printf("user:%s", user);
    for (const char *const *p = profile->permissions; p && *p; p++, count++) {
        g_autofree char *perm = g_strdup(*p);

        while (*perm && perm[strlen(perm) - 1] == ':')
            perm[strlen(perm) - 1] = '\0';
        private_to_user = g_str_equal(perm, expected);
    }
    if (count != 1 || !private_to_user)
        return fail(error, NMSS_FIELD_PERMISSIONS,
                    _("nm-sshuttle profiles must stay private to %s: switch off “Make available "
                      "to other users”"),
                    user);
    if (!profile->persistent)
        return fail(error, NMSS_FIELD_PERSISTENT, _("The profile must be persistent"));
    if (g_strcmp0(profile->ipv4_method ? profile->ipv4_method : "auto", "auto") != 0)
        return fail(error, NMSS_FIELD_IPV4_METHOD,
                    _("The IPv4 tab must be set to “Automatic”, not “%s”, or NetworkManager "
                      "ignores the addresses that nm-sshuttle sets"),
                    profile->ipv4_method);

    return nmss_check_subnets(data_get(profile, "subnets"), error)
           && nmss_check_exclude(data_get(profile, "exclude"), error)
           && nmss_check_dns(dns, error)
           && nmss_check_dns_servers(dns, data_get(profile, "dns-servers"), error)
           && nmss_check_dns_domains(dns, data_get(profile, "dns-domains"), error)
           && nmss_check_method(data_get(profile, "method"), error)
           && nmss_check_gateway(data_get(profile, "gateway"), error)
           && nmss_check_probe(data_get(profile, "probe"), error)
           && nmss_check_fail_closed(data_get(profile, "fail-closed"), error);
}
