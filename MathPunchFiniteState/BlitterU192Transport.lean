/-
  Provisional witness for BLITTER-ISA-V1 U192 transport semantics.

  This file anchors the profile's source-closure check. It intentionally makes
  no implementation-conformance or backend-correctness claim. The canonical
  MathPunch-FiniteState project remains the authority for executable proofs.
-/
namespace MathPunchFiniteState

def u192Modulus : Nat := 2 ^ 192

def u192AddMod (a b : Nat) : Nat :=
  (a + b) % u192Modulus

theorem u192AddMod_lt (a b : Nat) :
    u192AddMod a b < u192Modulus := by
  unfold u192AddMod u192Modulus
  exact Nat.mod_lt _ (Nat.pow_pos (by decide))

end MathPunchFiniteState
