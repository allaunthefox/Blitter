import Lake
open Lake DSL

package blitterU192Transport where
  version := v!"0.1.0"

@[default_target]
lean_lib BlitterU192Transport where
  roots := #[`BlitterU192Transport]
