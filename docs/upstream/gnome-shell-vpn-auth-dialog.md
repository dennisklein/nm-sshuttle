# networkAgent: VPN secrets request is never answered when the plugin's .name file has no auth-dialog

## Summary

libnm accepts a VPN plugin `.name` file that has no `[GNOME] auth-dialog=` key. For a connection that uses such a plugin, the source shows that GNOME Shell's network agent never answers a GetSecrets request for the `vpn` setting:

1. `NMVpnPluginInfo.get_auth_dialog()` returns `null`. libnm documents that it can.
2. `_findAuthBinary()` passes that value straight to `GLib.file_test()`.
3. GJS throws `Argument filename may not be null`, because `filename` is not a nullable argument.
4. The exception rejects the promise returned by `_vpnRequest()`, which nobody awaits or catches. So `respond()` is never called, and the caller waits until its own D-Bus timeout.

Steps 1–3 were reproduced with standalone gjs. In GNOME Shell 50.5 on Fedora 44 the visible end of step 4 was observed: an `Unhandled promise rejection` from `_vpnRequest()`, and a GetSecrets call that timed out after 25 s. The exception itself does not appear in the Shell's log. See "Observed vs. inferred".

The visible symptom is in GNOME Settings. Clicking the gear button of such a VPN starts the connection editor, which appeared only after about 25 s. In an earlier run, with the connection active, the editor showed a spinner.

The Shell already handles an auth-dialog binary that is missing or not executable: it logs the problem and responds `INTERNAL_ERROR`. Only the case where no auth dialog is configured skips that path.

## Versions

- **Observed on:** Fedora Linux 44 (Cloud Edition image with a GNOME session), GNOME Shell 50.5, NetworkManager 1.56.1-2.fc44, on 2026-10-07 and 2026-10-09. The GNOME Settings, gjs and GLib versions on that machine were not recorded.
- **Source read for this report:**
  - gnome-shell 50.5 (dd8bec9326c2) and gnome-shell main 29380c2662da (2026-10-06). In both, `js/ui/components/networkAgent.js` and `src/shell-network-agent.c` are byte-identical.
  - NetworkManager 1.56.1 (b829f838fc5d)
  - gjs 1.88.1 (90bc2a64443f)
  - GLib main (9235759daf6e)
  - gnome-control-center 50.4 (40a03b9566cc). Its `panels/network` is identical to the gnome-50 branch at ba4fb5b88837.
- **Standalone gjs checks (run, but not on the Fedora machine):** gjs 1.80.2, GLib 2.80.0 and libnm 1.46.0, from Ubuntu 24.04 packages.
- **History (read, not run):**
  - The unguarded `get_auth_dialog()` → `GLib.file_test()` call came in with 71515a8a1189, "networkAgent: Use libnm for plugin loading" (first tag: 3.28.1). There it sat in a synchronous loop over all plugins while the Shell built its VPN cache. The effect in versions 3.28.1 to 3.36.0 was not analysed.
  - The current per-request async form, where the exception becomes an unhandled rejection, comes from b97fc02e57da, "networkAgent: Make searching VPN binaries asynchronous" (first tag: 3.36.1).
  - Of these versions, only 50.5 was actually run.

## Steps to reproduce

> **This reproducer has not been run.** It is cut down from the observed case, which used a different plugin (see "Actual result"). Each step follows from the source.

You need a GNOME session in which GNOME Shell is the registered NetworkManager secret agent. The login keyring should be unlocked: the Shell agent searches the keyring before it reaches the faulty code, so a locked keyring may show an unlock prompt first. In the observed case no VPN service was running and the profile was not active.

1. As root, create a VPN plugin description with no `[GNOME]` section:

   ```sh
   sudo tee /usr/lib/NetworkManager/VPN/nm-noauth-repro.name >/dev/null <<'EOF'
   [VPN Connection]
   name=noauth-repro
   service=org.example.NoAuthRepro
   program=/usr/bin/false
   EOF
   ```

2. As the desktop user, create a profile of that type. Because the profile is restricted to your own user, GetSecrets needs only `settings.modify.own`. Upstream polkit policy allows that for an active session, so no prompt should appear.

   ```sh
   nmcli connection add type vpn con-name noauth-repro \
       vpn-type org.example.NoAuthRepro vpn.data 'foo = bar' \
       connection.permissions "user:$USER"
   ```

3. Open Settings → Network and click the gear button next to "noauth-repro" under VPN.

A terminal variant follows the same request path according to the source:

```sh
time nmcli --show-secrets connection show noauth-repro > /dev/null
```

To clean up:

```sh
nmcli connection delete noauth-repro
sudo rm /usr/lib/NetworkManager/VPN/nm-noauth-repro.name
```

The libnm and GJS parts can be checked in isolation. This was run on gjs 1.80.2 with the `.name` file from step 1:

```sh
$ gjs -c "imports.gi.versions.NM = '1.0'; const {NM} = imports.gi; print(NM.VpnPluginInfo.new_from_file(ARGV[0]).get_auth_dialog());" /usr/lib/NetworkManager/VPN/nm-noauth-repro.name
null
$ gjs -c "const {GLib} = imports.gi; GLib.file_test(null, GLib.FileTest.IS_EXECUTABLE);"
(gjs:8984): Gjs-CRITICAL **: …: JS ERROR: Error: Argument filename may not be null
```

## Actual result

**Observed (Fedora 44, GNOME Shell 50.5, NetworkManager 1.56.1-2.fc44, 2026-10-09).**
- **Setup.** The plugin was an sshuttle VPN plugin under development, `service=org.freedesktop.NetworkManager.sshuttle`. Its `.name` file, `/usr/lib/NetworkManager/VPN/nm-sshuttle-spike.name`, had only a `[VPN Connection]` section (name, service, program, supports-multiple-connections, supports-safe-private-file-access) and no `[GNOME]` section. Root had created the profile `nmss-spike` with `connection.permissions: user:tester`.
- **Connection state.** The profile was not active and no VPN service was running. It had been deactivated at 09:15:20, the plugin's service exited at 09:15:30, and nothing activated the profile again until 11:28:41. NetworkManager did not restart in between.
- **What the tester did.** The tester opened Settings (started at 09:26:49), went to Network and clicked the gear button of `nmss-spike` under VPN. Asked "How many seconds until the editor appears? (number, or 'never' after 60 s)", the tester answered "22", counted by hand.
- **Journal** (`journalctl _UID=1000`):

```
Oct 09 09:27:04 nmss-spike gnome-shell[1750]: Unhandled promise rejection. To suppress this warning, add an error handler to your promise chain with .catch() or a try-catch block around your await expression. Stack trace of the failed promise:
                                              _vpnRequest@resource:///org/gnome/shell/ui/components/networkAgent.js:829:22
                                              _handleRequest@resource:///org/gnome/shell/ui/components/networkAgent.js:806:18
                                              _newRequest@resource:///org/gnome/shell/ui/components/networkAgent.js:801:18
                                              @resource:///org/gnome/shell/ui/init.js:20:20
Oct 09 09:27:29 nmss-spike gnome-control-center[27305]: Failed to get secrets: Timeout was reached
Oct 09 09:27:29 nmss-spike gnome-control-center[27305]: vpn: (sshuttle,/usr/lib/NetworkManager/VPN/nm-sshuttle-spike.name) could not load plugin: missing "plugin" setting
```

- **Timing.** With `-o short-precise` the three messages carry 09:27:04.429224, 09:27:29.408332 and 09:27:29.408971. The GetSecrets call failed 24.98 s after the Shell's warning, which matches libnm's 25 s timeout. The tester's 22 s is a hand count of the same wait.
- **When the editor appeared (inferred).** The third message comes from the editor's VPN page. Its `finish_setup()` runs on the page's `initialized` signal and calls `vpn_get_plugin_by_service()`. That function loads the plugin list, which warns about the missing editor plugin (gnome-control-center 50.4 `ce-page-vpn.c:202-210`, `:226`; `vpn-helpers.c:43`, `:83`). So the page was set up right after the failed GetSecrets call. The Settings build on the machine was not recorded.
- **The stack.** Line 829 is the declaration `async _vpnRequest(`; lines 806 and 801 are the calls in `_handleRequest()` and `_newRequest()`. The trace shows where the rejected promise was created, not the line that threw. The warning has no exception text, and the journal has no "may not be null".
- **Control on the same machine and profile.** Settings was closed (09:27:43) and opened again (09:27:45). A few seconds later the `.name` file was rewritten with `[GNOME] auth-dialog=` naming a stub. The stub reads stdin up to `DONE`, prints two empty lines, waits for `QUIT` and exits 0. The tester then clicked the gear button again and said the editor appeared at once, with the tabs Details, Identity (a Name field and "unable to load VPN connection editor"), IPv4 and IPv6. GNOME Shell logged nothing. Settings logged:

```
Oct 09 09:27:59 nmss-spike gnome-control-center[27424]: Failed to get secrets: No agents were available for this request.
Oct 09 09:27:59 nmss-spike gnome-control-center[27424]: vpn: (sshuttle,/usr/lib/NetworkManager/VPN/nm-sshuttle-spike.name) could not load plugin: missing "plugin" setting
```

- **Why the control fails at once (source).** The stub's empty answer makes the Shell respond `CONFIRMED` with an empty `vpn` setting (`networkAgent.js:543-546`, `:524-530`; `src/shell-network-agent.c:481-509`). NetworkManager treats that as "agent returned no secrets" and tries the next agent (NetworkManager 1.56.1 `src/core/settings/nm-agent-manager.c:945-951`). With none left, it returns "No agents were available for this request." (`:826-833`).
- **Earlier observation (2026-10-07).** With the connection active, the tester reported "i see a spinner". How long it lasted was not recorded.
- **Not recorded.** NetworkManager ran at its default log level and logged nothing between 09:15:21 and 11:28:32, so its side of both requests is not visible.

**Observed in standalone gjs (gjs 1.80.2, not GNOME Shell).** A script copied the Shell's structure: `_handleRequest()` calls the async `_vpnRequest()` without awaiting it, and `file_test()` sits outside the `try`.
- The script printed only `Gjs-WARNING **: Unhandled promise rejection. To suppress this warning, … Stack trace of the failed promise: vpnRequest@…`, with no exception text.
- It never reached the stand-in for `respond()`.

**Expected from the source, not observed:**
- The exception behind the warning is `Argument filename may not be null`, thrown by `GLib.file_test(null, …)` at `networkAgent.js:860`.
- NetworkManager waits up to 120 s for the agent (`src/core/settings/nm-secret-agent.c:432`). A later request for the same connection and setting ends that wait sooner: the Shell agent cancels the pending request and answers it with `AGENT_CANCELED` (`src/shell-network-agent.c:365-372`, `:105-122`). In the run above, that probably happened at the second click.
- The `nmcli --show-secrets` variant takes about 25 s instead of returning at once.

## Expected result

The agent should answer at once with an error, as it already does when the auth-dialog binary is missing or not executable. The editor should then open without delay.

## Root cause

Everything in this section comes from reading the source, except where it says "observed". Line numbers are for gnome-shell 50.5, which match main 29380c2662da, unless another component is named.

1. **Settings asks for the `vpn` secrets before it shows the editor** (gnome-control-center 50.4, `panels/network/connection-editor/`).
   - The VPN page names `vpn` as its secrets setting (`ce-page-vpn.c:141-143`).
   - For an existing connection, `net-connection-editor.c:700-710` calls `get_secrets_for_page()`. That function calls `nm_remote_connection_get_secrets_async()` (`:604-620`).
   - The window shows an `Adw.Spinner` (`connection-editor.blp:39`) until every page has initialized (`net-connection-editor.c:534-539`).

2. **NetworkManager passes the request to the user's agents** (NetworkManager 1.56.1, `src/core/settings/`).
   - The D-Bus GetSecrets call sets `USER_REQUESTED | NO_ERRORS` (`nm-settings-connection.c:2002-2005`).
   - Agents are filtered by the connection's permission list and by the caller's uid (`nm-agent-manager.c:740-770`).
   - The connection has no stored `vpn` secrets, so the agent is asked (`nm-agent-manager.c:1193-1194`).

3. **`ShellNetworkAgent` checks the keyring, then sends every `vpn` request to the JavaScript side.**
   - `shell_network_agent_get_secrets()` searches the keyring with `SECRET_SEARCH_UNLOCK` (`src/shell-network-agent.c:399-406`). With `REQUEST_NEW`, or with always-ask secrets and interaction allowed, it goes straight to the UI instead (`:390-397`).
   - In the keyring callback, a request for the `vpn` setting is always passed on, whatever the flags (`:320-334`). It travels via the `new-request` signal (`:161-170`), which is connected to `_newRequest()` (`networkAgent.js:686`).

4. **The JavaScript side starts the request, but nothing handles its failure.**
   - Because `USER_REQUESTED` is set, `_newRequest()` calls `_handleRequest()` (`networkAgent.js:797-802`).
   - `_handleRequest()` calls the async `_vpnRequest()` without awaiting or catching it (`:805-806`).

5. **The auth-dialog path is null.**
   - In `_findAuthBinary()`, only `search_vpn_plugin()` is inside `try`/`catch` (`:852-857`).
   - `plugin.get_auth_dialog()` (`:859`) returns `NULL` when the `[GNOME] auth-dialog` key is missing or empty. See NetworkManager 1.56.1 `src/libnm-core-impl/nm-vpn-plugin-info.c:800-836`, whose doc comment says "the absolute path to the auth-dialog helper or %NULL". GJS turns `NULL` into `null`.
   - Observed with libnm 1.46.0 (see above).

6. **`GLib.file_test(null, …)` throws** (`:860`).
   - In GLib, `g_file_test`'s `filename` argument is annotated `(type filename)` with no `(nullable)` (`glib/gfileutils.c:259-261`). This was checked on GLib main, not on Fedora's build.
   - In gjs 1.88.1 a `null` value goes through `StringInTransferNone::in` (`gi/arg-cache.cpp:2034-2037`), then `NullableIn::in` (`:1189-1192`), then `Nullable::handle_nullable` (`:2024-2028`). It ends in `report_invalid_null`, which throws "Argument filename may not be null" (`:125-127`).
   - Observed on gjs 1.80.2 (see above).

7. **Nobody answers.**
   - The exception rejects the promise from `_findAuthBinary()`, and therefore the promise from `_vpnRequest()`, which has no handler.
   - The existing fallback at `:834-840` (log the problem, then respond `INTERNAL_ERROR`) is never reached, so the request stays pending in `ShellNetworkAgent`.
   - gjs only prints a generic warning with the promise's allocation stack (gjs 1.88.1 `gjs/engine.cpp:66-68`, `gjs/context.cpp:373-383`). The standalone gjs 1.80.2 script showed the same.

8. **Timeouts decide when the spinner stops.**
   - libnm's GetSecrets call uses `NM_DBUS_DEFAULT_TIMEOUT_MSEC`, which is 25000 (NetworkManager 1.56.1, `src/libnm-client-impl/nm-remote-connection.c:509-521` and `src/libnm-client-impl/nm-dbus-helpers.h:15`).
   - NetworkManager's call to the agent uses 120000 ms (`src/core/settings/nm-secret-agent.c:432`).
   - When the libnm call fails, Settings still adds the page: `net-connection-editor.c:581-600`, then `ce-page.c:118-126`, then `net-connection-editor.c:550-571`. So the editor appears after 25 s, as observed.

Settings is only the most visible client. Any request for the `vpn` setting of such a connection that reaches `_handleRequest()` takes the same path. A request without `USER_REQUESTED`, for example during activation, first shows a notification (`_showNotification()`). It reaches the faulty code only when the user clicks that notification.

## Proposed fix

The diff applies cleanly (`git apply --check`) to both 50.5 and main 29380c2662da. It has not been built or tested in GNOME Shell.

```diff
--- a/js/ui/components/networkAgent.js
+++ b/js/ui/components/networkAgent.js
@@ -857,6 +857,11 @@
         }
 
         const fileName = plugin.get_auth_dialog();
+        if (!fileName) {
+            log(`VPN plugin for ${serviceType} has no auth dialog`);
+            return null;
+        }
+
         if (!GLib.file_test(fileName, GLib.FileTest.IS_EXECUTABLE)) {
             log(`VPN plugin at ${fileName} is not executable`);
             return null;
```

With this change, `_vpnRequest()` takes its existing path: it logs "Invalid VPN service type (cannot find authentication binary)" and responds `INTERNAL_ERROR`, which the agent sends as `NM_SECRET_AGENT_ERROR_FAILED`. The rest of this paragraph comes from reading the NetworkManager 1.56.1 source:
- NetworkManager then tries the next agent (`_con_get_request_done()`, `nm-agent-manager.c:916-941`).
- If there is no other agent, it returns "No agents were available for this request." to the caller at once (`request_next_agent()`, `:826-832`).
- Settings would then log `Failed to get secrets: …` and open the editor immediately.

In the standalone gjs script, the same guard made both log messages appear and the `respond()` stand-in run.

**Optional hardening:** `_handleRequest()` could attach a `.catch()` to `_vpnRequest()` that logs the error and responds `INTERNAL_ERROR`. Any other exception in this async path would then fail the request instead of leaving it pending. It must not respond a second time to a request that `VPNRequestHandler` has already answered.

**Workaround for plugin authors (not tested):** ship an auth-dialog helper that implements the auth-dialog protocol and name it in `[GNOME] auth-dialog=` in the plugin's `.name` file. That avoids the null branch.

## Observed vs. inferred

- **Observed on Fedora 44 / GNOME Shell 50.5 (2026-10-09), with the profile inactive and no VPN service running:**
  - Clicking the gear button made GNOME Shell log `Unhandled promise rejection` with the stack `_vpnRequest` (`networkAgent.js:829`), `_handleRequest` (`:806`), `_newRequest` (`:801`), and no exception text.
  - Settings logged `Failed to get secrets: Timeout was reached` 24.98 s later. The tester counted 22 s by hand until the editor appeared.
  - On the same machine and profile, with `[GNOME] auth-dialog=` naming a stub that returns no secrets, GNOME Shell logged nothing. Settings logged `Failed to get secrets: No agents were available for this request.`, and the editor appeared at once.
- **Observed on Fedora 44 / GNOME Shell 50.5 (2026-10-07), with the profile active:** a spinner in the editor; its length was not recorded.
- **Observed in standalone gjs 1.80.2 / GLib 2.80.0 / libnm 1.46.0 (not GNOME Shell):**
  - `get_auth_dialog()` returns `null` for such a file.
  - `GLib.file_test(null, …)` throws `Argument filename may not be null`.
  - In a script with the same un-awaited async structure, the request is never answered, and the warning does not contain the exception text.
  - The proposed guard fixes that script.
- **Inferred from source:**
  - that the rejection in GNOME Shell is the `GLib.file_test(null, …)` exception at `networkAgent.js:860`, since the warning shows only where the promise was created
  - that the editor was built right after the failed GetSecrets call (from the timing of Settings' plugin warning)
  - the 120 s wait in NetworkManager, which logs nothing about agent requests at its default level
  - that the reproducer above (`org.example.NoAuthRepro`) and the nmcli variant behave the same way
  - that the proposed fix removes the delay. The stub run supports its last step: once NetworkManager answers "No agents were available for this request.", Settings shows the editor at once. The stub reaches that answer through an empty `CONFIRMED`; the fix would reach it through `INTERNAL_ERROR`.
- **Not checked:**
  - The reproducer as written has not been run.
  - The gjs, GLib and GNOME Settings builds on the Fedora machine, and any Fedora patches to them or to gnome-shell, are unknown.
  - Whether the login keyring was locked. No keyring prompt was reported.

<!-- Filer note: the GNOME GitLab issue tracker could not be reached from the environment this was written in (proxy returned 403), so it was not searched for duplicates. A general web search found no matching report. Please search GitLab before filing. -->
