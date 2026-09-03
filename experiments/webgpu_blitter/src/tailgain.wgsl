// tailgain.wgsl — exact tail-gain kernel.
//
// For a job (N2, prefix constraints):
//   G = max weight of a 3-AP-free subset A of [65, N2] such that
//       the prefix P union A is 3-AP-free,
//   weights in L_N2 = lcm(1..N2) units, 6x u32 words (192-bit, exact;
//   covers L_N2 < 2^192, i.e. N2 well beyond the sparse-checkpoint range).
//
// One thread per candidate mask over the d-element domain
// (element e in the domain <-> bit (e - 65)).

struct Job {
    d: u32,            // domain size (elements 65 .. 65+d-1)
    n_triples: u32,    // internal 3-AP masks over the domain
    n_pairs: u32,      // reflection pairs {b, 2b-p} (prefix-induced)
    blocked: u32,      // singles blocked by the prefix
    triples: array<u32, 512>,
    pairs: array<u32, 256>,
};

@group(0) @binding(0) var<storage, read> job: Job;
@group(0) @binding(1) var<storage, read> weights: array<u32>;   // [d][6]
@group(0) @binding(2) var<storage, read_write> out: array<u32>; // [2^d][7]: valid,w0..w5

fn add_word(a: u32, b: u32, carry: u32) -> vec2<u32> {
    let xy = a + b;
    let c_xy = u32(xy < a);
    let s = xy + carry;
    let c_xc = u32(s < xy);
    return vec2<u32>(s, c_xy | c_xc);
}

@compute @workgroup_size(64)
fn tail_gain_pass(@builtin(global_invocation_id) gid: vec3<u32>) {
    let idx = gid.x;
    if (idx >= (1u << job.d)) { return; }
    var mask = idx;
    if ((mask & job.blocked) != 0u) { return; }
    for (var i = 0u; i < job.n_pairs; i = i + 1u) {
        if ((mask & job.pairs[i]) == job.pairs[i]) { return; }
    }
    for (var i = 0u; i < job.n_triples; i = i + 1u) {
        if ((mask & job.triples[i]) == job.triples[i]) { return; }
    }
    var w0 = 0u;
    var w1 = 0u;
    var w2 = 0u;
    var w3 = 0u;
    var w4 = 0u;
    var w5 = 0u;
    for (var e = 0u; e < job.d; e = e + 1u) {
        if (((mask >> e) & 1u) == 0u) { continue; }
        let b0 = weights[e * 6u + 0u];
        let b1 = weights[e * 6u + 1u];
        let b2 = weights[e * 6u + 2u];
        let b3 = weights[e * 6u + 3u];
        let b4 = weights[e * 6u + 4u];
        let b5 = weights[e * 6u + 5u];
        let r0 = add_word(w0, b0, 0u);
        w0 = r0.x;
        let r1 = add_word(w1, b1, r0.y);
        w1 = r1.x;
        let r2 = add_word(w2, b2, r1.y);
        w2 = r2.x;
        let r3 = add_word(w3, b3, r2.y);
        w3 = r3.x;
        let r4 = add_word(w4, b4, r3.y);
        w4 = r4.x;
        let r5 = add_word(w5, b5, r4.y);
        w5 = r5.x;
        // r5.y (final carry) cannot occur: max score < 2^175 << 2^192
    }
    let base = idx * 7u;
    out[base + 0u] = 1u;    // valid
    out[base + 1u] = w0;
    out[base + 2u] = w1;
    out[base + 3u] = w2;
    out[base + 4u] = w3;
    out[base + 5u] = w4;
    out[base + 6u] = w5;
}
