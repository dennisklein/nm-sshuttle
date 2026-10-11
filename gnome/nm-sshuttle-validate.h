/* SPDX-License-Identifier: MIT */
/*
 * The profile rules of design 4.2, in C. They mirror nm_sshuttle/profile.py,
 * and tests/profile-cases.json holds the cases that both must agree on.
 *
 * Uses GLib only, so that the test can run without GTK or NetworkManager.
 */
#ifndef NM_SSHUTTLE_VALIDATE_H
#define NM_SSHUTTLE_VALIDATE_H

#include <glib.h>

G_BEGIN_DECLS

#define NMSS_VALIDATE_ERROR (nmss_validate_error_quark())
GQuark nmss_validate_error_quark(void);

/* The error code of a failed check: the field that is wrong. */
typedef enum {
    NMSS_FIELD_REMOTE,
    NMSS_FIELD_LOCAL_USER,
    NMSS_FIELD_PERMISSIONS,
    NMSS_FIELD_PERSISTENT,
    NMSS_FIELD_IPV4_METHOD,
    NMSS_FIELD_SUBNETS,
    NMSS_FIELD_EXCLUDE,
    NMSS_FIELD_DNS,
    NMSS_FIELD_DNS_SERVERS,
    NMSS_FIELD_DNS_DOMAINS,
    NMSS_FIELD_METHOD,
    NMSS_FIELD_GATEWAY,
    NMSS_FIELD_PROBE,
    NMSS_FIELD_FAIL_CLOSED,
} NmssField;

/* "remote", "connection.permissions" and so on, as profile.py names them. */
const char *nmss_field_key(NmssField field);

/* Checks of one field. A NULL value is a key that is not there. */
gboolean nmss_check_remote(const char *value, GError **error);
gboolean nmss_check_local_user(const char *value, GError **error);
gboolean nmss_check_subnets(const char *value, GError **error);
gboolean nmss_check_exclude(const char *value, GError **error);
gboolean nmss_check_dns(const char *value, GError **error);
/* These two depend on the dns mode (NULL: none). */
gboolean nmss_check_dns_servers(const char *dns, const char *value, GError **error);
gboolean nmss_check_dns_domains(const char *dns, const char *value, GError **error);
gboolean nmss_check_method(const char *value, GError **error);
gboolean nmss_check_gateway(const char *value, GError **error);
gboolean nmss_check_probe(const char *value, GError **error);
gboolean nmss_check_fail_closed(const char *value, GError **error);

/* The words of a list, separated by one space as the CLI writes them. */
char *nmss_normalize_list(const char *value);

typedef struct {
    GHashTable *data;               /* vpn.data: char * to char * */
    const char *const *permissions; /* connection.permissions as "user:alice", NULL-terminated */
    gboolean persistent;            /* vpn.persistent */
    const char *ipv4_method;        /* ipv4.method; NULL when unset */
} NmssProfile;

/*
 * Everything profile.parse checks, in its order, except that the local user
 * is not looked up: whether it exists and is a regular user is for the
 * plugin to decide.
 */
gboolean nmss_profile_validate(const NmssProfile *profile, GError **error);

G_END_DECLS

#endif
