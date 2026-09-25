# Triaged bugs

- Fixed: Tenderly failure strings reported as success. Converting a non-empty status
  string such as `"failed"` with `bool()` marked reverted bundle calls as successful.
