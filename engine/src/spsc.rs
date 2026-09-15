//! Lock-free single-producer single-consumer ring buffer for `Copy` messages.
//!
//! Head and tail live on separate cache lines, and each side keeps a cached copy of the other's index
//! so it only reads the shared atomic when the ring looks full or empty.

use std::cell::UnsafeCell;
use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

// 128 bytes, not 64. Adjacent-line prefetch on recent x86 pulls lines in pairs, so 64 still false-shares.
#[repr(align(128))]
struct Padded<T>(T);

struct Ring<T> {
    head: Padded<AtomicUsize>,
    tail: Padded<AtomicUsize>,
    mask: usize,
    buf: Box<[UnsafeCell<MaybeUninit<T>>]>,
}

// Safe because exactly one Producer and one Consumer exist, and each slot is handed between them by the
// release store and acquire load on head and tail.
unsafe impl<T: Send> Sync for Ring<T> {}
unsafe impl<T: Send> Send for Ring<T> {}

pub struct Producer<T> {
    ring: Arc<Ring<T>>,
    tail: usize,
    head_cache: usize,
}

pub struct Consumer<T> {
    ring: Arc<Ring<T>>,
    head: usize,
    tail_cache: usize,
}

/// A ring with room for `cap` messages, rounded up to a power of two.
pub fn channel<T: Copy + Send>(cap: usize) -> (Producer<T>, Consumer<T>) {
    let cap = cap.next_power_of_two();
    let buf = (0..cap).map(|_| UnsafeCell::new(MaybeUninit::uninit())).collect();
    let ring = Arc::new(Ring { head: Padded(AtomicUsize::new(0)), tail: Padded(AtomicUsize::new(0)), mask: cap - 1, buf });
    (Producer { ring: ring.clone(), tail: 0, head_cache: 0 }, Consumer { ring, head: 0, tail_cache: 0 })
}

impl<T: Copy + Send> Producer<T> {
    /// Returns the message back if the ring is full.
    #[inline]
    pub fn try_push(&mut self, v: T) -> Result<(), T> {
        let cap = self.ring.mask + 1;
        if self.tail - self.head_cache == cap {
            self.head_cache = self.ring.head.0.load(Ordering::Acquire);
            if self.tail - self.head_cache == cap {
                return Err(v);
            }
        }
        unsafe { (*self.ring.buf[self.tail & self.ring.mask].get()).write(v) };
        self.tail += 1;
        self.ring.tail.0.store(self.tail, Ordering::Release);
        Ok(())
    }

    /// Spin until there is room. The matcher side never blocks, so spinning here is bounded by its speed.
    #[inline]
    pub fn push(&mut self, mut v: T) {
        loop {
            match self.try_push(v) {
                Ok(()) => return,
                Err(back) => {
                    v = back;
                    std::hint::spin_loop();
                }
            }
        }
    }
}

impl<T: Copy + Send> Consumer<T> {
    #[inline]
    pub fn try_pop(&mut self) -> Option<T> {
        if self.head == self.tail_cache {
            self.tail_cache = self.ring.tail.0.load(Ordering::Acquire);
            if self.head == self.tail_cache {
                return None;
            }
        }
        let v = unsafe { (*self.ring.buf[self.head & self.ring.mask].get()).assume_init() };
        self.head += 1;
        self.ring.head.0.store(self.head, Ordering::Release);
        Some(v)
    }

    #[inline]
    pub fn pop(&mut self) -> T {
        loop {
            if let Some(v) = self.try_pop() {
                return v;
            }
            std::hint::spin_loop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delivers_every_message_once_and_in_order_across_threads() {
        let (mut p, mut c) = channel::<u64>(64);
        let n = 2_000_000u64;
        let t = std::thread::spawn(move || {
            for i in 0..n {
                p.push(i);
            }
        });
        for i in 0..n {
            assert_eq!(c.pop(), i);
        }
        t.join().unwrap();
        assert!(c.try_pop().is_none());
    }

    #[test]
    fn reports_full_and_empty() {
        let (mut p, mut c) = channel::<u32>(4);
        for i in 0..4 {
            p.try_push(i).unwrap();
        }
        assert_eq!(p.try_push(9), Err(9));
        assert_eq!(c.try_pop(), Some(0));
        p.try_push(4).unwrap();
        assert_eq!((1..5).map(|_| c.try_pop().unwrap()).collect::<Vec<_>>(), vec![1, 2, 3, 4]);
        assert_eq!(c.try_pop(), None);
    }
}
