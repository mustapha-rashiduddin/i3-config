# AGENTS.md

## Lessons Learned

### Speed

- **Perceived latency is all that matters.** Users care about the gap between action and visible feedback, not total execution time. Move work before the user-visible action whenever possible (e.g. xprop before dmenu).
- **Subprocess spawning is the silent killer.** Each `subprocess.run()` costs ~30-50ms on NixOS for fork+exec alone. Four calls = 200ms+ of dead time. Eliminate subprocess calls from critical paths entirely.
- **Python startup is expensive.** The interpreter alone costs ~50-100ms. For latency-critical scripts, use shell or compiled tools.
- **Move work out of the critical path.** Never make the user wait for work they don't need to see. Do lookups before the action, saves after via async mechanisms (pipes, inotify).

### Race Conditions

- **EOF on FIFOs is sticky.** Once a writer closes and the reader hits EOF, `select()` stops waking up for new writers on that fd. Fix: close and reopen the FIFO after each read.
- **Generation counters prevent stale overwrites.** Use counters (`_focus_gen`, `_overlay_gen`) so background threads can detect when their data went stale during subprocess calls, and discard the result.
- **In-place mutation + atomic snapshot swap.** Main thread patches dicts directly (fast). Background thread builds a new snapshot and swaps it atomically. Prevents tearing.
- **Cross-filesystem `mv` doesn't trigger inotify reliably.** `/tmp` to home directory = copy+delete, not rename. `IN_MOVED_TO` may not fire. Use same-directory temp files for inotify-based refresh.
- **Test assumptions, don't trust docs.** `i3-msg nop` does not generate tick events. Raw IPC socket ticks don't deliver to subscribers. Verify with manual testing.

### i3 Specifics

- `i3-msg nop` does NOT generate tick events.
- `i3-msg -t send_tick` DOES work for subscriber delivery.
- Raw IPC socket tick (type 7) does NOT deliver to i3 subscribers.
- When moving a container to another workspace, i3 does NOT focus that workspace. Focus stays on the current workspace.
- When switching to a new workspace, i3 fires `workspace::init` then `workspace::focus` events. Both may arrive in the same `os.read()` buffer.
- Workspace names from i3 events match the `set $wsN` value exactly (e.g. `;` not `4:;`).
- `os.inotify_init()` was removed in Python 3.14. Use `ctypes.CDLL("libc.so.6").inotify_init()` instead.
- `os.fdopen()` wrapping an inotify fd breaks `select()`. Pass the raw int fd to select, not file objects.
