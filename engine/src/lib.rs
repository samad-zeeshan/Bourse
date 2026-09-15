//! Bourse matching engine: a price-time priority book, its matcher, and the tools that check it.

pub mod affinity;
pub mod bitset;
pub mod book;
pub mod check;
pub mod codec;
pub mod gen;
pub mod idmap;
pub mod pipeline;
pub mod pyref;
pub mod spsc;
pub mod types;

pub use book::{Book, Config};
pub use types::*;
