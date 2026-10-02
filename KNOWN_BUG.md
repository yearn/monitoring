# Known bugs

- **Resolved — oversized individual stale-operation alerts:** The ten-call cap did not
  bound decoded signature lengths, allowing Telegram to truncate details before caching;
  call details now fit the message budget with an explicit omitted-call count, and
  oversized entries are rejected before delivery.
