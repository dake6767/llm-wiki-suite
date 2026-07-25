//! Child-process helpers shared by pack probes and model prefetch.

use std::process::Command;

/// The Browser is a windowed process, so on Windows every child it spawns opens
/// its own console window. Setup runs probes and the Python model prefetch in
/// the background: a black console popping up mid-install looks like something
/// broke, and users do not know whether closing it aborts the download.
pub(crate) fn hide_console(command: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt as _;

        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}
