//! Fixed-capacity map from order id to slab slot, sized once at startup so it never allocates.

const EMPTY: u64 = u64::MAX;

pub struct IdMap {
    keys: Box<[u64]>,
    vals: Box<[u32]>,
    mask: usize,
    shift: u32,
    len: usize,
}

impl IdMap {
    /// Holds at least `cap` live ids. The table is at least twice that so probe runs stay short.
    pub fn with_capacity(cap: usize) -> IdMap {
        let size = (cap.max(8) * 2).next_power_of_two();
        IdMap {
            keys: vec![EMPTY; size].into_boxed_slice(),
            vals: vec![0; size].into_boxed_slice(),
            mask: size - 1,
            shift: 64 - size.trailing_zeros(),
            len: 0,
        }
    }

    // Fibonacci hashing. Client ids are often sequential, and the multiply spreads them across the table.
    #[inline(always)]
    fn home(&self, key: u64) -> usize {
        (key.wrapping_mul(0x9E37_79B9_7F4A_7C15) >> self.shift) as usize
    }

    #[inline]
    pub fn get(&self, key: u64) -> Option<u32> {
        // The empty marker would otherwise match the first free slot it probes.
        if key == EMPTY {
            return None;
        }
        let mut i = self.home(key);
        loop {
            let k = self.keys[i];
            if k == key {
                return Some(self.vals[i]);
            }
            if k == EMPTY {
                return None;
            }
            i = (i + 1) & self.mask;
        }
    }

    /// Caller guarantees the key is absent, `u64::MAX` is never used as an id, and the map is not full.
    #[inline]
    pub fn insert(&mut self, key: u64, val: u32) {
        debug_assert!(key != EMPTY);
        let mut i = self.home(key);
        while self.keys[i] != EMPTY {
            i = (i + 1) & self.mask;
        }
        self.keys[i] = key;
        self.vals[i] = val;
        self.len += 1;
    }

    #[inline]
    pub fn remove(&mut self, key: u64) -> Option<u32> {
        if key == EMPTY {
            return None;
        }
        let mut i = self.home(key);
        loop {
            let k = self.keys[i];
            if k == EMPTY {
                return None;
            }
            if k == key {
                break;
            }
            i = (i + 1) & self.mask;
        }
        let val = self.vals[i];
        // Backward-shift deletion instead of tombstones. Tombstones would pile up under constant
        // add and cancel traffic and make every lookup slower until a rehash, which would allocate.
        let mut hole = i;
        let mut j = (i + 1) & self.mask;
        while self.keys[j] != EMPTY {
            let h = self.home(self.keys[j]);
            // Move j into the hole unless its home lies cyclically inside (hole, j].
            let between = if hole <= j { hole < h && h <= j } else { hole < h || h <= j };
            if !between {
                self.keys[hole] = self.keys[j];
                self.vals[hole] = self.vals[j];
                hole = j;
            }
            j = (j + 1) & self.mask;
        }
        self.keys[hole] = EMPTY;
        self.len -= 1;
        Some(val)
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn matches_std_hashmap_under_churn() {
        let mut m = IdMap::with_capacity(64);
        let mut r: HashMap<u64, u32> = HashMap::new();
        let mut x: u64 = 12345;
        for step in 0..200_000u32 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let key = x % 200;
            if r.contains_key(&key) {
                assert_eq!(m.remove(key), r.remove(&key));
            } else if r.len() < 64 {
                m.insert(key, step);
                r.insert(key, step);
            }
            assert_eq!(m.get(key), r.get(&key).copied());
            assert_eq!(m.len(), r.len());
        }
        for (k, v) in &r {
            assert_eq!(m.get(*k), Some(*v));
        }
    }
}
