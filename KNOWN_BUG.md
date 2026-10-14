# Triaged bugs

- Lido metadata interpreted as Telegram Markdown (fixed in PR #423): creator-supplied vote metadata was interpolated without escaping, allowing formatting errors and disguised inline links.
- Long Lido metadata lost entirely (fixed in PR #423): the whole description was one entry in a formatter that drops oversized entries instead of retaining a preview.
- Empty Lido vote ID header (fixed in PR #423): the vote ID line was appended before checking whether the event contained an ID.
