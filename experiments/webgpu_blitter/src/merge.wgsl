// merge.wgsl — exact sharded merge kernel.
//
// Each thread owns one prefix in the slice and scans the suffix list
// (pre-sorted by descending exact score) for the FIRST compatible suffix:
//   compatible  <=>  (suffix_mask & blocked) == 0
//                     and (prefix & FE(suffix)) == 0
// where blocked are the singles blocked by the prefix and FE are the
// prefix elements forbidden by the suffix's pairs.
//
// The total = prefix weight + suffix weight, 6x u32 words (192-bit exact,
// L_N2 units).  The empty suffix (mask 0, FE 0) is the final entry and is
// always compatible, so every prefix is assigned.
//
// Bit widths: suffix masks/blocked <= 31 bits (d <= 31 -> N2 <= 95);
// prefix masks and FE carry bits up to 41 -> split into lo/hi u32.

struct MergeJob {
    n_suffix: u32,
    slice_start: u32,
    slice_count: u32,
};

@group(0) @binding(0) var<storage, read> job: MergeJob;
@group(0) @binding(1) var<storage, read> p_masks: array<u32>;  // [slice][2] lo,hi
@group(0) @binding(2) var<storage, read> p_blocked: array<u32>;
@group(0) @binding(3) var<storage, read> p_w: array<u32>;      // [slice][6]
@group(0) @binding(4) var<storage, read> s_masks: array<u32>;
@group(0) @binding(5) var<storage, read> s_fe: array<u32>;     // [n_suffix][2] lo,hi
@group(0) @binding(6) var<storage, read> s_w: array<u32>;      // [n_suffix][6]
@group(0) @binding(7) var<storage, read_write> out: array<u32>; // [slice][8]

fn add_word(a: u32, b: u32, carry: u32) -> vec2<u32> {
    let xy = a + b;
    let c_xy = u32(xy < a);
    let s = xy + carry;
    let c_xc = u32(s < xy);
    return vec2<u32>(s, c_xy | c_xc);
}

@compute @workgroup_size(64)
fn merge_pass(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= job.slice_count) { return; }
    let p = i;
    let mlo = p_masks[p * 2u + 0u];
    let mhi = p_masks[p * 2u + 1u];
    let blk = p_blocked[p];
    for (var s = 0u; s < job.n_suffix; s = s + 1u) {
        let sm = s_masks[s];
        if ((sm & blk) != 0u) { continue; }
        let f0 = s_fe[s * 2u + 0u];
        let f1 = s_fe[s * 2u + 1u];
        if (((mlo & f0) != 0u) || ((mhi & f1) != 0u)) { continue; }
        // compatible: exact 6-word sum of prefix and suffix weights
        var r = add_word(p_w[p * 6u + 0u], s_w[s * 6u + 0u], 0u);
        var w0 = r.x;
        var c = r.y;
        r = add_word(p_w[p * 6u + 1u], s_w[s * 6u + 1u], c);
        var w1 = r.x;
        c = r.y;
        r = add_word(p_w[p * 6u + 2u], s_w[s * 6u + 2u], c);
        var w2 = r.x;
        c = r.y;
        r = add_word(p_w[p * 6u + 3u], s_w[s * 6u + 3u], c);
        var w3 = r.x;
        c = r.y;
        r = add_word(p_w[p * 6u + 4u], s_w[s * 6u + 4u], c);
        var w4 = r.x;
        c = r.y;
        r = add_word(p_w[p * 6u + 5u], s_w[s * 6u + 5u], c);
        var w5 = r.x;
        let base = p * 8u;
        out[base + 0u] = 1u;
        out[base + 1u] = w0;
        out[base + 2u] = w1;
        out[base + 3u] = w2;
        out[base + 4u] = w3;
        out[base + 5u] = w4;
        out[base + 6u] = w5;
        out[base + 7u] = 0u;
        return;
    }
    // unreachable: the empty suffix (mask 0, FE 0) is always compatible
    let base = p * 8u;
    out[base + 0u] = 0u;
}
