// WGSL: full Laurent product pipeline over blitter surfaces (prototype v2).
// Stage 1: products (A x B) -> surface B texels (qe, te, coef, valid).
// Stage 2 (host): blit surface B -> prods buffer (same 16-byte layout).
// Stage 3: bitonic sort by packed key u64 = (qe << 32) | te, single workgroup N=64.
// Stage 4: reduce (fold equal keys) + compact (drop zero coeffs) into finalTerms.
// Exact integer arithmetic only.

struct Term { qe: u32, te: u32, coef: i32, valid: u32 }

@group(0) @binding(0) var<storage, read> termsB : array<Term>;
@group(0) @binding(1) var surfaceA : texture_2d<u32>;
@group(0) @binding(2) var surfaceB : texture_storage_2d<rgba32uint, write>;
@group(0) @binding(3) var<storage, read_write> prods : array<Term>;
@group(0) @binding(4) var<storage, read_write> finalTerms : array<Term>;
@group(0) @binding(5) var<storage, read_write> counters : array<atomic<u32>>;
@group(0) @binding(6) var<storage, read_write> finalOut : array<Term>; // [outCount]

const N : u32 = 64u;

// ---------- stage 1: products ----------
@compute @workgroup_size(64)
fn product_pass(@builtin(global_invocation_id) gid : vec3<u32>) {
    let idx = gid.x;
    if (idx >= N) { return; }
    let w = 8u;
    let i = idx % w;
    let j = idx / w;
    let a = textureLoad(surfaceA, vec2<i32>(i32(i), 0), 0);
    if (a.w == 0u) { textureStore(surfaceB, vec2<i32>(i32(idx), 0), vec4<u32>(0u,0u,0u,0u)); return; }
    let b = termsB[j];
    if (b.valid == 0u) { textureStore(surfaceB, vec2<i32>(i32(idx), 0), vec4<u32>(0u,0u,0u,0u)); return; }
    let c = i32(a.z) * b.coef;
    textureStore(surfaceB, vec2<i32>(i32(idx), 0), vec4<u32>(a.x + b.qe, a.y + b.te, bitcast<u32>(c), 1u));
}

// ---------- stage 3: bitonic sort (lexicographic (qe,te), no u64 needed) ----------
var<workgroup> wq : array<u32, 64>;
var<workgroup> wt : array<u32, 64>;
var<workgroup> wcoef : array<i32, 64>;

fn keyLess(lq : u32, lt : u32, rq : u32, rt : u32) -> bool {
    return (lq < rq) || ((lq == rq) && (lt < rt));
}
fn keyGreater(lq : u32, lt : u32, rq : u32, rt : u32) -> bool {
    return (lq > rq) || ((lq == rq) && (lt > rt));
}

@compute @workgroup_size(64)
fn sort_pass(@builtin(global_invocation_id) gid : vec3<u32>) {
    let lane = gid.x;
    if (lane >= N) { return; }
    if (prods[lane].valid == 0u) { wq[lane] = 0xffffffffu; wt[lane] = 0xffffffffu; wcoef[lane] = 0; }
    else { wq[lane] = prods[lane].qe; wt[lane] = prods[lane].te; wcoef[lane] = prods[lane].coef; }
    workgroupBarrier();
    var k : u32 = 2u;
    while (k <= N) {
        var j : u32 = k >> 1u;
        while (j > 0u) {
            let other = lane ^ j;
            if (other > lane) {
                let dirFlag = select(0u, 1u, (lane & k) == 0u);
                let lq = wq[lane]; let lt = wt[lane]; let lcoef = wcoef[lane];
                let oq = wq[other]; let ot = wt[other]; let ocoef = wcoef[other];
                let ascending : bool = dirFlag == 1u;
                let swap : bool = (ascending && keyGreater(lq, lt, oq, ot)) || ((!ascending) && keyLess(lq, lt, oq, ot));
                if (swap) {
                    wq[lane] = oq; wt[lane] = ot; wcoef[lane] = ocoef;
                    wq[other] = lq; wt[other] = lt; wcoef[other] = lcoef;
                }
            }
            workgroupBarrier();
            j = j >> 1u;
        }
        k = k << 1u;
    }
    prods[lane].qe = wq[lane];
    prods[lane].te = wt[lane];
    prods[lane].coef = wcoef[lane];
    // validity travels WITH the key, not the position: any non-sentinel key is a
    // real product (positions that were invalid pre-sort receive valid products).
    prods[lane].valid = select(0u, 1u, wq[lane] != 0xffffffffu || wt[lane] != 0xffffffffu);
}

// ---------- stage 4: reduce ----------
@compute @workgroup_size(64)
fn reduce_pass(@builtin(global_invocation_id) gid : vec3<u32>) {
    let lane = gid.x;
    if (lane >= N) { return; }
    if (prods[lane].valid == 0u) { return; }
    let isStart = (lane == 0u)
        || prods[lane-1u].valid == 0u
        || prods[lane].qe != prods[lane-1u].qe
        || prods[lane].te != prods[lane-1u].te;
    if (isStart) {
        var acc : i32 = prods[lane].coef;
        var j = lane + 1u;
        while (j < N) {
            if (prods[j].valid == 0u || prods[j].qe != prods[lane].qe || prods[j].te != prods[lane].te) { break; }
            acc = acc + prods[j].coef;
            j = j + 1u;
        }
        finalTerms[lane] = Term(prods[lane].qe, prods[lane].te, acc, 1u);
    }
}

var<workgroup> wfinal : array<Term, 64>;

@compute @workgroup_size(64)
fn compact_pass(@builtin(global_invocation_id) gid : vec3<u32>) {
    let lane = gid.x;
    if (lane >= N) { wfinal[lane] = Term(0u,0u,0,0u); return; }
    wfinal[lane] = finalTerms[lane];
    workgroupBarrier();
    if (wfinal[lane].valid == 0u) { return; }
    if (wfinal[lane].coef == 0) { return; }
    let qe = wfinal[lane].qe; let te = wfinal[lane].te; let cf = wfinal[lane].coef;
    var slot = atomicAdd(&counters[0], 1u);
    finalOut[slot] = Term(qe, te, cf, 1u);
}
