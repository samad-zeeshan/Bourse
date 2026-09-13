//! Checks the hot-path promise: once the book is built and warm, processing orders never allocates.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicU64, Ordering};

use bourse::gen::FlowGen;
use bourse::*;

struct Counting;

static ALLOCS: AtomicU64 = AtomicU64::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        ALLOCS.fetch_add(1, Ordering::Relaxed);
        unsafe { System.alloc(l) }
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        unsafe { System.dealloc(p, l) }
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        ALLOCS.fetch_add(1, Ordering::Relaxed);
        unsafe { System.realloc(p, l, n) }
    }
}

#[global_allocator]
static A: Counting = Counting;

#[test]
fn two_million_orders_after_warm_up_allocate_nothing() {
    let cfg = Config { min_price: 0, levels: 1 << 14, max_orders: 1 << 18, stp: Stp::CancelResting };
    let mut book = Book::new(cfg);
    let mut g = FlowGen::new(42, 0, 1 << 14);
    let warm: Vec<Command> = (0..200_000).map(|_| g.next_command()).collect();
    let hot: Vec<Command> = (0..2_000_000).map(|_| g.next_command()).collect();
    let mut sink = CountingSink::default();
    for c in &warm {
        book.process(c, &mut sink);
    }
    let before = ALLOCS.load(Ordering::Relaxed);
    for c in &hot {
        book.process(c, &mut sink);
    }
    let after = ALLOCS.load(Ordering::Relaxed);
    assert!(sink.trades > 10_000, "the flow should trade, got {}", sink.trades);
    assert_eq!(after - before, 0, "allocations on the hot path");
    book.check_structure().unwrap();
}
