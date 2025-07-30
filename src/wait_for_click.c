#include <libinput.h>
#include <libudev.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static int open_restricted(const char *path, int flags, void *user_data) {
    return open(path, flags);
}

static void close_restricted(int fd, void *user_data) {
    close(fd);
}

static const struct libinput_interface interface = {
    .open_restricted = open_restricted,
    .close_restricted = close_restricted,
};

int main(void) {
    struct udev *udev = udev_new();
    if (!udev) {
        fprintf(stderr, "Failed to create udev context\n");
        return 1;
    }

    struct libinput *li = libinput_udev_create_context(&interface, NULL, udev);
    if (!li) {
        fprintf(stderr, "Failed to create libinput context\n");
        udev_unref(udev);
        return 1;
    }

    if (libinput_udev_assign_seat(li, "seat0") != 0) {
        fprintf(stderr, "Failed to assign seat\n");
        libinput_unref(li);
        udev_unref(udev);
        return 1;
    }

    struct pollfd fds[] = {{
        .fd = libinput_get_fd(li),
        .events = POLLIN,
    }};

    while (1) {
        int ret = poll(fds, 1, -1);
        if (ret <= 0) {
            // Poll error or interrupted
            continue;
        }

        libinput_dispatch(li);

        struct libinput_event *event;
        while ((event = libinput_get_event(li)) != NULL) {
            enum libinput_event_type type = libinput_event_get_type(event);

            if (type == LIBINPUT_EVENT_POINTER_BUTTON) {
                struct libinput_event_pointer *ev = libinput_event_get_pointer_event(event);
                uint32_t button = libinput_event_pointer_get_button(ev);
                uint32_t state = libinput_event_pointer_get_button_state(ev);

                if (state == LIBINPUT_BUTTON_STATE_PRESSED) {
                    printf("Detected click: button %u\n", button);
                    fflush(stdout);  // Make sure output is sent immediately
                }
            }

            libinput_event_destroy(event);
        }
    }

    // Cleanup never reached, but good practice
    libinput_unref(li);
    udev_unref(udev);

    return 0;
}
