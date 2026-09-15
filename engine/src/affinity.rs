//! Pin the calling thread to one logical CPU.
//!
//! Windows only for now. Elsewhere it reports that it did nothing, and the caller records that.

#[cfg(windows)]
mod win {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn GetCurrentThread() -> isize;
        pub fn SetThreadAffinityMask(thread: isize, mask: usize) -> usize;
        pub fn SetThreadPriority(thread: isize, priority: i32) -> i32;
    }
}

/// Returns true if the thread is now pinned to `cpu`.
pub fn pin_current(cpu: usize) -> bool {
    #[cfg(windows)]
    unsafe {
        let h = win::GetCurrentThread();
        // THREAD_PRIORITY_TIME_CRITICAL. It cuts how often the scheduler parks the measured thread.
        win::SetThreadPriority(h, 15);
        win::SetThreadAffinityMask(h, 1usize << cpu) != 0
    }
    #[cfg(not(windows))]
    {
        let _ = cpu;
        false
    }
}
