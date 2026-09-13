//! Two-level bitset over price levels, used to find the next non-empty level after the best one empties.

pub struct LevelBits {
    words: Box<[u64]>,
    summary: Box<[u64]>,
}

impl LevelBits {
    pub fn new(n: usize) -> LevelBits {
        let words = n.div_ceil(64);
        LevelBits { words: vec![0; words].into_boxed_slice(), summary: vec![0; words.div_ceil(64)].into_boxed_slice() }
    }

    #[inline]
    pub fn set(&mut self, i: usize) {
        self.words[i >> 6] |= 1 << (i & 63);
        self.summary[i >> 12] |= 1 << ((i >> 6) & 63);
    }

    #[inline]
    pub fn clear(&mut self, i: usize) {
        let w = i >> 6;
        self.words[w] &= !(1 << (i & 63));
        if self.words[w] == 0 {
            self.summary[w >> 6] &= !(1 << (w & 63));
        }
    }

    /// Lowest set index at or above `from`.
    #[inline]
    pub fn next_up(&self, from: usize) -> Option<usize> {
        let mut w = from >> 6;
        if w >= self.words.len() {
            return None;
        }
        let bits = self.words[w] & (u64::MAX << (from & 63));
        if bits != 0 {
            return Some((w << 6) | bits.trailing_zeros() as usize);
        }
        // Skip whole 64-word blocks through the summary instead of walking empty words one by one.
        w += 1;
        let mut s = w >> 6;
        if s >= self.summary.len() {
            return None;
        }
        let mut sbits = if w & 63 == 0 { self.summary[s] } else { self.summary[s] & (u64::MAX << (w & 63)) };
        loop {
            if sbits != 0 {
                let wi = (s << 6) | sbits.trailing_zeros() as usize;
                return Some((wi << 6) | self.words[wi].trailing_zeros() as usize);
            }
            s += 1;
            if s >= self.summary.len() {
                return None;
            }
            sbits = self.summary[s];
        }
    }

    /// Highest set index at or below `from`.
    #[inline]
    pub fn next_down(&self, from: usize) -> Option<usize> {
        let w = from >> 6;
        let bits = self.words[w] & (u64::MAX >> (63 - (from & 63)));
        if bits != 0 {
            return Some((w << 6) | (63 - bits.leading_zeros() as usize));
        }
        if w == 0 {
            return None;
        }
        let w = w - 1;
        let mut s = w >> 6;
        let mut sbits = self.summary[s] & (u64::MAX >> (63 - (w & 63)));
        loop {
            if sbits != 0 {
                let wi = (s << 6) | (63 - sbits.leading_zeros() as usize);
                return Some((wi << 6) | (63 - self.words[wi].leading_zeros() as usize));
            }
            if s == 0 {
                return None;
            }
            s -= 1;
            sbits = self.summary[s];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agrees_with_a_linear_scan() {
        let n = 20_000;
        let mut b = LevelBits::new(n);
        let mut v = vec![false; n];
        let mut x: u64 = 99;
        for _ in 0..50_000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let i = (x % n as u64) as usize;
            if v[i] {
                b.clear(i);
                v[i] = false;
            } else if x % 7 < 3 {
                b.set(i);
                v[i] = true;
            }
            let from = ((x >> 20) % n as u64) as usize;
            assert_eq!(b.next_up(from), (from..n).find(|&j| v[j]));
            assert_eq!(b.next_down(from), (0..=from).rev().find(|&j| v[j]));
        }
    }
}
