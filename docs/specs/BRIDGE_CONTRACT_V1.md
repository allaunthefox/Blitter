# Bridge Contract v1

**Status:** FROZEN  
**Normative authority:** Subordinate to `docs/specs/PASS_INTERFACE_V1.md`. Every bridge between MathPunch passes must conform to this contract.

A bridge translates the output IR of one pass into the input IR of another. The current bridges are **adapters** — they execute and pass data between nodes. A hardened bridge must additionally prove that the translation preserves a declared semantic relation.

## What a bridge must prove

Every bridge must declare and verify:

1. **Source denotation** — what the upstream pass's output IR *means* (the preserved property from its contract)
2. **Target precondition** — what the downstream pass's input IR *requires* (the preconditions from its contract)
3. **Translation relation** — a mathematical statement connecting source denotation to target precondition
4. **Schema validation** — every field in the translated output is either (a) directly carried from the source, (b) derived via a declared rule, or (c) set to a declared default. No field may appear without a documented origin.
5. **Fail-closed propagation** — if the upstream pass failed, the bridge must propagate failure (not silently default). If the translation is impossible, the bridge must reject (not silently approximate).
6. **Hostile fixtures** — the bridge must be tested against at least:
   - Empty/null upstream output
   - Malformed upstream output (missing required fields)
   - Stale upstream certificate hash
   - Wrong IR type for target pass
   - Default values that silently change semantics

## Bridge classifications

| Class | What it proves | When to use |
|---|---|---|
| **IDENTITY** | source_denotation = target_denotation | Same IR, same semantics (e.g., pass chain within same kind) |
| **EMBEDDING** | source_denotation ⊆ target_denotation | Source domain is subset of target domain |
| **EXTRACTION** | target_precondition ⟸ source_denotation | Derived parameter from semantic output |
| **ADAPTER** | No denotation theorem claimed | Current state — honest about being an adapter |

## Hardening a bridge

To harden an ADAPTER bridge:

1. Write the denotation claim — what mathematical relationship holds between source and target
2. Document every field's origin (carried, derived, defaulted)
3. Test with hostile fixtures (empty, malformed, stale)
4. Upgrade classification from ADAPTER to EXTRACTION/EMBEDDING/IDENTITY
5. Pin the bridge contract with a versioned hash

## Existing bridges (to harden)

### Bridge A: braid-word → matrix-word-search (currently ADAPTER)
- Source denotation: Hachimoji canonical string preserves LKB signature Q_4(w)
- Target precondition: matrix-word search requires generators, target, length, probes
- Translation claim: word_length is carried from source; matrix target and probes are **declared constants** (not derived from source). This bridge does NOT claim that the Hachimoji output influences the matrix search.
- Classification after hardening: EXTRACTION (word_length extracted; target/probes are declared constants)
- Acceptable because: the elimination pass is independent of the braid word — it searches for ANY words that produce the target. Bridge A's honesty is that it doesn't pretend otherwise.

### Bridge B: matrix-word-solutions → binomial-divisor-parameters (currently EXTRACTION)
- Source denotation: exact solution set (Sol_pass = Sol_baseline) of matrix words
- Target precondition: N_min, N_max, k, prime_set
- Translation claim: k = word_length (carried); N_max = solution_count * M (derived, ensuring non-empty progression); prime_set = [2, 3] (declared constants)
- Classification after hardening: EXTRACTION
- Add: hostile test for zero solutions → N_max still produces non-empty progression
