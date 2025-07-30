#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <poll.h>
#include <stdbool.h>
#include <wayland-client.h>
#include <ctype.h>
#include <dirent.h> // Required for directory operations
#include "../gen/wlr-foreign-toplevel-management-unstable-v1-client-protocol.h"

// --- Structs & Globals ---
struct client_state; // Forward declaration

// --- Struct for tracking processed apps during a QUERY ---
struct processed_app_id {
    char *app_id;
    struct wl_list link;
};

// --- NEW: Struct to hold data from .desktop files ---
struct desktop_app {
    char *app_id;
    char *name;
    char *generic_name;
    char *icon;
    char *bin;
    char *actions;
    struct wl_list link;
};

struct toplevel {
    uint32_t id;
    char *title;
    char *app_id;
    struct zwlr_foreign_toplevel_handle_v1 *handle;
    uint32_t window_state;
    struct client_state *state;
    struct wl_list link;
};

struct client_state {
    struct wl_display *wl_display;
    struct zwlr_foreign_toplevel_manager_v1 *toplevel_manager;
    struct wl_seat *wl_seat; // Required for ACTIVATE command
    struct wl_list toplevels;
    struct wl_list desktop_apps; // NEW: To store data from QUERY
};

static uint32_t next_toplevel_id = 0;

// --- Helper Functions (No changes here) ---
char *get_field_from_desktop_file(const char *app_id, const char *field_name) {
    if (!app_id) return NULL;
    char desktop_path[1024];
    const char *home_dir = getenv("HOME");
    char home_desktop_path_buffer[1024];
    if (home_dir) {
        snprintf(home_desktop_path_buffer, sizeof(home_desktop_path_buffer), "%s/.local/share/applications/%s.desktop", home_dir, app_id);
    }
    const char *possible_filenames[] = {
        "/usr/share/applications/%s.desktop", "/var/lib/flatpak/exports/share/applications/%s.desktop",
        home_dir ? home_desktop_path_buffer : NULL, NULL
    };
    FILE *f = NULL;
    for (int i = 0; possible_filenames[i] != NULL && f == NULL; ++i) {
        snprintf(desktop_path, sizeof(desktop_path), possible_filenames[i], app_id);
        f = fopen(desktop_path, "r");
    }
    char *found_value = NULL;
    if (f) {
        char *line = NULL; size_t len = 0;
        size_t field_len = strlen(field_name);
        while (getline(&line, &len, f) != -1) {
            if (strncmp(line, field_name, field_len) == 0 && line[field_len] == '=') {
                char *value = line + field_len + 1;
                value[strcspn(value, "\n")] = 0;
                if (strcmp(field_name, "Exec") == 0) {
                    char *percent = strchr(value, '%');
                    if (percent) {
                        char *temp = percent;
                        while(temp > value && *(temp-1) == ' '){ temp--; }
                        *temp = '\0';
                    }
                }
                found_value = strdup(value);
                break;
            }
        }
        free(line);
        fclose(f);
    }
    return found_value;
}

char *get_actions_from_desktop_file(const char *app_id) {
    if (!app_id) return NULL;
    char desktop_path[1024];
    const char *home_dir = getenv("HOME");
    char home_desktop_path_buffer[1024];
    if (home_dir) {
        snprintf(home_desktop_path_buffer, sizeof(home_desktop_path_buffer), "%s/.local/share/applications/%s.desktop", home_dir, app_id);
    }
    const char *possible_filenames[] = {
        "/usr/share/applications/%s.desktop", "/var/lib/flatpak/exports/share/applications/%s.desktop",
        home_dir ? home_desktop_path_buffer : NULL, NULL
    };
    FILE *f = NULL;
    for (int i = 0; possible_filenames[i] != NULL && f == NULL; ++i) {
        snprintf(desktop_path, sizeof(desktop_path), possible_filenames[i], app_id);
        f = fopen(desktop_path, "r");
    }
    if (!f) return NULL;
    char *line = NULL, *actions_list_str = NULL, *final_output = NULL;
    size_t len = 0;
    while (getline(&line, &len, f) != -1) {
        if (strncmp(line, "Actions=", 8) == 0) {
            actions_list_str = strdup(line + 8);
            actions_list_str[strcspn(actions_list_str, "\n")] = 0;
            break;
        }
    }
    if (!actions_list_str || strlen(actions_list_str) == 0) {
        fclose(f); free(line); return NULL;
    }
    size_t output_size = 4096;
    final_output = malloc(output_size);
    final_output[0] = '\0';
    char *actions_copy = strdup(actions_list_str);
    char *action_id = strtok(actions_copy, ";");
    while(action_id && strlen(action_id) > 0) {
        char section_header[256];
        snprintf(section_header, sizeof(section_header), "[Desktop Action %s]", action_id);
        rewind(f);
        char *action_name = NULL, *action_exec = NULL;
        int in_section = 0;
        while(getline(&line, &len, f) != -1) {
            char *trimmed_line = line;
            while(isspace((unsigned char)*trimmed_line)) trimmed_line++;
            trimmed_line[strcspn(trimmed_line, "\n")] = 0;
            if (strcmp(trimmed_line, section_header) == 0) { in_section = 1; continue; }
            if (in_section) {
                if (trimmed_line[0] == '[') break;
                if (strncmp(trimmed_line, "Name=", 5) == 0 && !action_name) action_name = strdup(trimmed_line + 5);
                else if (strncmp(trimmed_line, "Exec=", 5) == 0 && !action_exec) action_exec = strdup(trimmed_line + 5);
            }
            if (action_name && action_exec) break;
        }
        if (action_name && action_exec) {
            char entry[1024];
            snprintf(entry, sizeof(entry), "%s|%s;", action_name, action_exec);
            if (strlen(final_output) + strlen(entry) < output_size) strcat(final_output, entry);
        }
        free(action_name); free(action_exec);
        action_id = strtok(NULL, ";");
    }
    free(actions_list_str); free(actions_copy); free(line); fclose(f);
    if (strlen(final_output) > 0) final_output[strlen(final_output) - 1] = '\0';
    return final_output;
}

void format_state_string(uint32_t state, char* buffer, size_t buffer_len) {
    buffer[0] = '\0';
    if (state & ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_MAXIMIZED) strncat(buffer, "Maximized ", buffer_len - strlen(buffer) - 1);
    if (state & ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_MINIMIZED) strncat(buffer, "Minimized ", buffer_len - strlen(buffer) - 1);
    if (state & ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_ACTIVATED) strncat(buffer, "Active ", buffer_len - strlen(buffer) - 1);
    if (state & ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_FULLSCREEN) strncat(buffer, "Fullscreen ", buffer_len - strlen(buffer) - 1);
    if (buffer[0] == '\0') strncat(buffer, "Normal", buffer_len - strlen(buffer) - 1);
    else {
        size_t len = strlen(buffer);
        if (len > 0 && buffer[len - 1] == ' ') buffer[len - 1] = '\0';
    }
}

// --- Scans directories, prints and caches all found desktop files ---
void query_desktop_files(struct client_state *state) {
    struct wl_list processed_app_ids;
    wl_list_init(&processed_app_ids);

    const char *home_dir = getenv("HOME");
    char home_desktop_path_buffer[1024];
    if (home_dir) {
        snprintf(home_desktop_path_buffer, sizeof(home_desktop_path_buffer), "%s/.local/share/applications", home_dir);
    }

    const char *app_dirs[] = {
        home_dir ? home_desktop_path_buffer : NULL,
        "/usr/share/applications",
        "/var/lib/flatpak/exports/share/applications",
        NULL
    };

    for (int i = 0; app_dirs[i] != NULL; ++i) {
        DIR *d = opendir(app_dirs[i]);
        if (!d) continue;

        struct dirent *dir;
        while ((dir = readdir(d)) != NULL) {
            const char *name = dir->d_name;
            const char *desktop_suffix = ".desktop";
            size_t name_len = strlen(name);
            size_t suffix_len = strlen(desktop_suffix);

            if (name_len > suffix_len && strcmp(name + name_len - suffix_len, desktop_suffix) == 0) {
                char *app_id = strndup(name, name_len - suffix_len);

                bool is_processed = false;
                struct processed_app_id *p_app;
                wl_list_for_each(p_app, &processed_app_ids, link) {
                    if (strcmp(p_app->app_id, app_id) == 0) {
                        is_processed = true;
                        break;
                    }
                }

                if (!is_processed) {
                    struct processed_app_id *new_p_app = malloc(sizeof(struct processed_app_id));
                    new_p_app->app_id = strdup(app_id);
                    wl_list_insert(&processed_app_ids, &new_p_app->link);

                    char *app_name = get_field_from_desktop_file(app_id, "Name");
                    char *generic_name = get_field_from_desktop_file(app_id, "GenericName");
                    char *icon_name = get_field_from_desktop_file(app_id, "Icon");
                    char *bin_path = get_field_from_desktop_file(app_id, "Exec");
                    char *actions_str = get_actions_from_desktop_file(app_id);

                    printf("DB APPID=\"%s\" NAME=\"%s\" GENERIC_NAME=\"%s\" ICON=\"%s\" BIN=\"%s\" ACTIONS=\"%s\"\n",
                           app_id,
                           app_name ? app_name : "",
                           generic_name ? generic_name : "",
                           icon_name ? icon_name : "",
                           bin_path ? bin_path : "",
                           actions_str ? actions_str : "");
                    fflush(stdout);

                    // NEW: Store in our in-memory database
                    struct desktop_app *new_db_app = calloc(1, sizeof(struct desktop_app));
                    new_db_app->app_id = strdup(app_id);
                    new_db_app->name = app_name ? strdup(app_name) : strdup("");
                    new_db_app->generic_name = generic_name ? strdup(generic_name) : strdup("");
                    new_db_app->icon = icon_name ? strdup(icon_name) : strdup("");
                    new_db_app->bin = bin_path ? strdup(bin_path) : strdup("");
                    new_db_app->actions = actions_str ? strdup(actions_str) : strdup("");
                    wl_list_insert(&state->desktop_apps, &new_db_app->link);

                    free(app_name);
                    free(generic_name);
                    free(icon_name);
                    free(bin_path);
                    free(actions_str);
                }
                free(app_id);
            }
        }
        closedir(d);
    }

    struct processed_app_id *p_app, *tmp;
    wl_list_for_each_safe(p_app, tmp, &processed_app_ids, link) {
        wl_list_remove(&p_app->link);
        free(p_app->app_id);
        free(p_app);
    }
}

// --- Wayland Listener Callbacks ---
static void toplevel_handle_title(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, const char *title) {
    struct toplevel *toplevel = data; free(toplevel->title); toplevel->title = strdup(title);
}
static void toplevel_handle_app_id(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, const char *app_id) {
    struct toplevel *toplevel = data; free(toplevel->app_id); toplevel->app_id = strdup(app_id);
}
static void toplevel_handle_state(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_array *s) {
    struct toplevel *toplevel = data; toplevel->window_state = 0; uint32_t *entry;
    wl_array_for_each(entry, s) { toplevel->window_state |= *entry; }
}

static void toplevel_handle_done(void *data, struct zwlr_foreign_toplevel_handle_v1 *h) {
    struct toplevel *toplevel = data;
    struct client_state *state = toplevel->state;

    // --- NEW MATCHING LOGIC ---
    char *final_app_id = toplevel->app_id;
    bool direct_match_found = false;

    // Phase 1: Try for a direct match with the Wayland-provided app_id
    if (toplevel->app_id) {
        struct desktop_app *app;
        wl_list_for_each(app, &state->desktop_apps, link) {
            if (strcmp(app->app_id, toplevel->app_id) == 0) {
                direct_match_found = true;
                break;
            }
        }
    }

    // Phase 2: If no direct match, fallback to matching by the window title
    if (!direct_match_found && toplevel->title) {
        struct desktop_app *app;
        wl_list_for_each(app, &state->desktop_apps, link) {
            if (app->name && strcmp(app->name, "") != 0 && strcmp(app->name, toplevel->title) == 0) {
                final_app_id = app->app_id; // Success! Use the correct app_id from the .desktop file
                break;
            }
        }
    }
    // --- END OF NEW LOGIC ---

    char state_str[256];
    format_state_string(toplevel->window_state, state_str, sizeof(state_str));

    printf("UPDATE ID=%u APPID=\"%s\" STATE=\"%s\" TITLE=\"%s\"\n",
           toplevel->id,
           final_app_id ? final_app_id : "", // Use the potentially corrected app_id
           state_str,
           toplevel->title ? toplevel->title : "");
    fflush(stdout);
}

static void toplevel_handle_closed(void *data, struct zwlr_foreign_toplevel_handle_v1 *h) {
    struct toplevel *toplevel = data;
    printf("CLOSED ID=%u\n", toplevel->id); fflush(stdout);
    wl_list_remove(&toplevel->link);
    zwlr_foreign_toplevel_handle_v1_destroy(toplevel->handle);
    free(toplevel->title); free(toplevel->app_id); free(toplevel);
}
static void toplevel_handle_output_enter(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_output *o) {}
static void toplevel_handle_output_leave(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_output *o) {}
static void toplevel_handle_parent(void *data, struct zwlr_foreign_toplevel_handle_v1 *h, struct zwlr_foreign_toplevel_handle_v1 *p) {}

static const struct zwlr_foreign_toplevel_handle_v1_listener toplevel_handle_listener = {
    .title = toplevel_handle_title, .app_id = toplevel_handle_app_id, .output_enter = toplevel_handle_output_enter,
    .output_leave = toplevel_handle_output_leave, .state = toplevel_handle_state, .done = toplevel_handle_done,
    .closed = toplevel_handle_closed, .parent = toplevel_handle_parent,
};

static void toplevel_manager_handle_toplevel(void *data, struct zwlr_foreign_toplevel_manager_v1 *m, struct zwlr_foreign_toplevel_handle_v1 *handle) {
    struct client_state *state = data;
    struct toplevel *toplevel = calloc(1, sizeof(struct toplevel));
    toplevel->id = next_toplevel_id++;
    toplevel->handle = handle;
    toplevel->state = state;

    wl_list_init(&toplevel->link);
    wl_list_insert(&state->toplevels, &toplevel->link);

    zwlr_foreign_toplevel_handle_v1_add_listener(handle, &toplevel_handle_listener, toplevel);

    printf("NEW ID=%u\n", toplevel->id);
    fflush(stdout);
}

static void toplevel_manager_handle_finished(void *data, struct zwlr_foreign_toplevel_manager_v1 *m) {}

static const struct zwlr_foreign_toplevel_manager_v1_listener toplevel_manager_listener = {
    .toplevel = toplevel_manager_handle_toplevel, .finished = toplevel_manager_handle_finished,
};

static void registry_handle_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    struct client_state *state = data;
    if (strcmp(interface, zwlr_foreign_toplevel_manager_v1_interface.name) == 0) {
        state->toplevel_manager = wl_registry_bind(registry, name, &zwlr_foreign_toplevel_manager_v1_interface, 3);
        zwlr_foreign_toplevel_manager_v1_add_listener(state->toplevel_manager, &toplevel_manager_listener, state);
    } else if (strcmp(interface, wl_seat_interface.name) == 0) {
        state->wl_seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
    }
}
static void registry_handle_global_remove(void *data, struct wl_registry *registry, uint32_t name) {}

static const struct wl_registry_listener registry_listener = {
    .global = registry_handle_global, .global_remove = registry_handle_global_remove,
};

// --- Command Handling ---
void handle_command(struct client_state *state, char *command) {
    char *cmd = strtok(command, " \n");
    if (!cmd) return;

    if (strcmp(cmd, "QUERY") == 0) {
        query_desktop_files(state); // Pass state to the function
        printf("QUERY_DONE\n");
        fflush(stdout);
        return;
    }

    if (strcmp(cmd, "MINIMIZEALL") == 0) {
        struct toplevel *t;
        wl_list_for_each(t, &state->toplevels, link) {
            zwlr_foreign_toplevel_handle_v1_set_minimized(t->handle);
        }
        return;
    }
    
    char *id_str = strtok(NULL, " \n");
    if (!id_str) return;
    int id = atoi(id_str);
    
    struct toplevel *target = NULL, *t;
    wl_list_for_each(t, &state->toplevels, link) {
        if (t->id == id) { 
            target = t; 
            break; 
        }
    }

    if (!target) return;

    if (strcmp(cmd, "ACTIVATE") == 0) {
        if (state->wl_seat) {
            zwlr_foreign_toplevel_handle_v1_activate(target->handle, state->wl_seat);
        }
    }
    else if (strcmp(cmd, "MINIMIZE") == 0) zwlr_foreign_toplevel_handle_v1_set_minimized(target->handle);
    else if (strcmp(cmd, "UNMINIMIZE") == 0) zwlr_foreign_toplevel_handle_v1_unset_minimized(target->handle);
    else if (strcmp(cmd, "CLOSE") == 0) zwlr_foreign_toplevel_handle_v1_close(target->handle);
}

// --- Main ---
int main(int argc, char **argv) {
    struct client_state state = { 0 };
    wl_list_init(&state.toplevels);
    wl_list_init(&state.desktop_apps); // Initialize the new list
    state.wl_display = wl_display_connect(NULL);
    if (!state.wl_display) {
        fprintf(stderr, "Failed to connect to Wayland display.\n");
        return 1;
    }

    struct wl_registry *registry = wl_display_get_registry(state.wl_display);
    wl_registry_add_listener(registry, &registry_listener, &state);

    wl_display_roundtrip(state.wl_display);
    wl_display_roundtrip(state.wl_display);

    printf("DAEMON_READY\n");
    fflush(stdout);

    struct pollfd fds[2];
    fds[0].fd = wl_display_get_fd(state.wl_display);
    fds[0].events = POLLIN;
    fds[1].fd = fileno(stdin);
    fds[1].events = POLLIN;

    while (1) {
        while (wl_display_prepare_read(state.wl_display) != 0) {
            wl_display_dispatch_pending(state.wl_display);
        }
        wl_display_flush(state.wl_display);
        int ret = poll(fds, 2, -1);
        if (ret < 0) {
            wl_display_cancel_read(state.wl_display);
            break;
        }
        if (fds[0].revents & POLLIN) {
            wl_display_read_events(state.wl_display);
            wl_display_dispatch_pending(state.wl_display);
        } else {
            wl_display_cancel_read(state.wl_display);
        }
        if (fds[1].revents & POLLIN) {
            char *line = NULL;
            size_t len = 0;
            if (getline(&line, &len, stdin) != -1) {
                handle_command(&state, line);
            }
            free(line);
            if (feof(stdin)) {
                break;
            }
        }
    }

    // Clean up the cached desktop_apps list
    struct desktop_app *app, *tmp;
    wl_list_for_each_safe(app, tmp, &state.desktop_apps, link) {
        wl_list_remove(&app->link);
        free(app->app_id);
        free(app->name);
        free(app->generic_name);
        free(app->icon);
        free(app->bin);
        free(app->actions);
        free(app);
    }
    
    wl_display_disconnect(state.wl_display);
    return 0;
}