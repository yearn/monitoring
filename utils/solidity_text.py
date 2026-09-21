"""Brace-aware scanning of a *single* Solidity source file.

Every helper here takes the text of one file and, where it matters, the name of
one contract inside it. That scoping is the point: an Etherscan verification
bundle contains the deployed contract plus its bases, its libraries and every
interface it imports, so text extracted from the concatenated bundle cannot be
attributed to the deployed contract (see ``utils.impl_diff``).

Two same-length maskings of the source are used, so byte offsets found in one
are valid in the other and in the original text:

- :func:`strip_noise` blanks comments *and* string literals. Scanning runs on
  this, so a ``{`` or the word ``function`` inside a string cannot break brace
  matching, and commented-out code is never mistaken for deployed code.
- :func:`strip_comments` blanks only comments. Captured header/body text comes
  from this, so a changed revert message, role identifier or token name is a
  real difference rather than an invisible one.
"""

import re
from dataclasses import dataclass

# Comment / string-literal spans, replaced by same-length whitespace.
_NOISE_RE = re.compile(
    r'/\*[\s\S]*?\*/|//[^\n]*|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'',
)

# `contract Foo`, `abstract contract Foo`, `library Foo`, `interface Foo`.
_DECL_KINDS = "contract|library|interface"

# Parameter data-location keywords, dropped when normalizing a parameter to its type.
_DATA_LOCATIONS = frozenset({"memory", "calldata", "storage"})

# Header tokens that are neither visibility nor a meaningful modifier.
_HEADER_NOISE = frozenset({"virtual", "override"})

_VISIBILITIES = frozenset({"external", "public", "internal", "private"})

_ANY_DECLARATION_RE = re.compile(rf"(?:^|[\s;}}])(?:abstract\s+)?({_DECL_KINDS})\s+(\w+)\b[^{{;]*\{{")

# `type Id is bytes32;` — a user-defined value type, file- or contract-level.
_UDVT_RE = re.compile(r"\btype\s+(\w+)\s+is\s+(\w+)\s*;")

# One entry of an `is` clause: `Base`, `Base(arg)`, or a qualified `Lib.Base`.
_BASE_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")

_STRUCT_RE = re.compile(r"\bstruct\s+\w+\s*\{")

# `@custom:storage-location erc7201:openzeppelin.storage.Initializable` — the
# formula prefix is kept, so namespaces under different formulas never collide.
_STORAGE_LOCATION_RE = re.compile(r"@custom:storage-location\s+([\w-]+:[^\s*]+)")

# Solidity type aliases, expanded so `uint` and `uint256` key the same function.
_TYPE_ALIASES = {"uint": "uint256", "int": "int256", "byte": "bytes1"}
_ALIAS_RE = re.compile(r"^(?:" + "|".join(_TYPE_ALIASES) + r")(?=$|\[)")

# A function-like definition: `function foo(`, `constructor(`, `modifier m(`,
# `receive()`, `fallback()`. A function *type* (`function (uint) external cb;`)
# has no identifier after the keyword, so it never matches.
_FUNCTION_START_RE = re.compile(
    r"\b(?:(function|modifier)\s+(\w+)|(constructor|receive|fallback))\s*\(",
)


@dataclass(frozen=True)
class FunctionDef:
    """One function/modifier/constructor defined directly in a contract body."""

    name: str
    kind: str  # "function" / "modifier" / "constructor" / "receive" / "fallback"
    params: str  # normalized parameter types, e.g. "address,uint256"
    visibility: str  # "external" / "public" / "internal" / "private" or ""
    modifiers: tuple[str, ...]  # source-level modifiers (onlyOwner, view, payable, …)
    body: str  # whitespace-normalized body, "" for a declaration with no body
    has_body: bool
    header: str = ""  # whitespace-normalized text between the params and the body
    span: tuple[int, int] = (0, 0)  # (start, end) of the definition within the scanned text
    literals: tuple[str, ...] = ()  # string literals in the definition, verbatim and in order

    @property
    def signature(self) -> str:
        """`name(types)` — stable across the two sides of an upgrade diff."""
        return f"{self.name}({self.params})"

    @property
    def fingerprint(self) -> str:
        """What must match for behavior to be unchanged.

        Comments are gone and whitespace is collapsed, so reflowing a function
        or editing a docstring is not a change. Everything else is: a changed
        call, modifier, numeric literal — or string literal, which `body` keeps
        and `literals` pins byte-for-byte, since whitespace inside a string is
        data, not formatting.
        """
        return f"{self.header}|{self.body}|{'|'.join(self.literals)}"


def strip_noise(source: str) -> str:
    """Blank out comments and string literals, preserving length and line breaks.

    For structural scanning only: string contents are gone, so never compare
    two versions of a function on this text (see :func:`strip_comments`).
    """
    return _NOISE_RE.sub(lambda m: _blank(m.group(0)), source)


def strip_comments(source: str) -> str:
    """Blank out comments, keeping string literals intact and offsets stable.

    The same single pass recognizes both, so a ``//`` inside a string literal
    stays part of the string rather than eating the rest of the line.
    """
    return _NOISE_RE.sub(lambda m: _blank(m.group(0)) if _is_comment(m.group(0)) else m.group(0), source)


def _blank(text: str) -> str:
    """Same-length whitespace, keeping newlines so line numbers stay put."""
    return "".join("\n" if c == "\n" else " " for c in text)


def _is_comment(text: str) -> bool:
    return text.startswith(("//", "/*"))


def _string_literals(text: str) -> tuple[str, ...]:
    """Every string literal in ``text``, verbatim and in source order."""
    return tuple(m.group(0) for m in _NOISE_RE.finditer(text) if not _is_comment(m.group(0)))


def declares_contract(source: str, name: str) -> bool:
    """True if this file declares ``contract``/``library``/``interface`` ``name``."""
    return _find_declaration(strip_noise(source), name) is not None


def declared_names(source: str) -> list[str]:
    """Every contract, library and interface this file declares, in order."""
    return [name for name, _ in declarations(source)]


def declarations(source: str) -> list[tuple[str, str]]:
    """(name, kind) for every declaration in the file; kind is contract/library/interface."""
    return [(m.group(2), m.group(1)) for m in _ANY_DECLARATION_RE.finditer(strip_noise(source))]


def value_type_declarations(source: str) -> list[tuple[str, str]]:
    """(name, underlying type) for every ``type X is T;`` in the file.

    The underlying type is alias-normalized (``uint`` → ``uint256``), so two
    spellings of the same representation compare equal.
    """
    return [
        (m.group(1), _ALIAS_RE.sub(lambda a: _TYPE_ALIASES[a.group(0)], m.group(2)))
        for m in _UDVT_RE.finditer(strip_noise(source))
    ]


def find_contract_span(source: str, name: str) -> tuple[int, int] | None:
    """Offsets of ``name``'s body within ``source`` (between its braces), or None."""
    cleaned = strip_noise(source)
    open_brace = _find_declaration(cleaned, name)
    if open_brace is None:
        return None
    end = _match_brace(cleaned, open_brace)
    return None if end is None else (open_brace + 1, end)


def find_contract_body(source: str, name: str) -> str | None:
    """Return the noise-stripped body of ``name``'s declaration, or None.

    The returned text is everything between the declaration's braces, so a
    caller scanning it only ever sees members of that one contract — not of its
    bases, not of imported interfaces.
    """
    span = find_contract_span(source, name)
    return None if span is None else strip_noise(source)[span[0] : span[1]]


def parent_names(source: str, name: str) -> list[str]:
    """Direct bases of ``name``, from its ``is A, B(args), C`` clause, in order.

    Constructor arguments passed in the clause are dropped. Returns [] when the
    contract has no bases or isn't declared in ``source``.
    """
    cleaned = strip_noise(source)
    match = _declaration_match(cleaned, name)
    if match is None:
        return []
    clause = re.search(r"\bis\b(.*)", match.group(1), re.S)
    if clause is None:
        return []
    names: list[str] = []
    for part in _split_top_level(clause.group(1)):
        base = _BASE_NAME_RE.match(part.strip())
        if base:
            # `is Lib.Base` names the contract declared as `Base`.
            names.append(base.group(0).split(".")[-1])
    return names


def namespaced_structs(source: str, name: str) -> dict[str, str]:
    """ERC-7201 namespaces declared inside ``name``: namespace id → struct text.

    ERC-7201 puts ``@custom:storage-location erc7201:<id>`` on the *struct*
    holding the namespace, inside the contract — not on the contract itself —
    so every struct in the body is checked for it. The returned text is the
    struct definition with comments removed and whitespace collapsed, so two
    versions compare equal exactly when their definitions do.
    """
    span = find_contract_span(source, name)
    if span is None:
        return {}
    cleaned = strip_noise(source)
    content = strip_comments(source)
    out: dict[str, str] = {}
    for m in _STRUCT_RE.finditer(cleaned, span[0], span[1]):
        open_brace = m.end() - 1
        close = _match_brace(cleaned, open_brace)
        if close is None:
            continue
        # The struct's natspec is the comment run between the previous statement
        # and the `struct` keyword — read from the original, where it survives.
        preceding = cleaned[span[0] : m.start()]
        start = span[0] + max(preceding.rfind("}"), preceding.rfind(";"), preceding.rfind("{")) + 1
        location = _STORAGE_LOCATION_RE.search(source[start : m.start()])
        if location:
            out[location.group(1)] = " ".join(content[m.start() : close + 1].split())
    return out


def contract_functions(source: str, name: str) -> list[FunctionDef] | None:
    """Functions defined directly in ``name``, or None if it isn't in ``source``.

    The one entry point callers should need: it scopes to the contract, scans
    the masked text, captures content from the string-preserving text, and
    returns spans that index straight back into ``source``.
    """
    span = find_contract_span(source, name)
    if span is None:
        return None
    start, end = span
    return iter_functions(
        strip_noise(source)[start:end],
        strip_comments(source)[start:end],
        offset=start,
    )


def iter_functions(contract_body: str, content_body: str | None = None, offset: int = 0) -> list[FunctionDef]:
    """Extract the function-like members declared directly in ``contract_body``.

    ``contract_body`` must come from :func:`find_contract_body` — it is scanned
    for structure, so its string literals are blanked. ``content_body`` is the
    same range with string literals intact (:func:`strip_comments`); header and
    body text are read from it so string changes are visible. It defaults to
    ``contract_body``, which is fine for structural tests but blind to string
    edits — prefer :func:`contract_functions`.

    ``offset`` is added to each span so callers can index the original source.
    Matches nested deeper than the contract's own member level (inside a
    function body, an assembly block, a struct) are skipped.
    """
    content = contract_body if content_body is None else content_body
    depths = _brace_depths(contract_body)
    out: list[FunctionDef] = []
    for m in _FUNCTION_START_RE.finditer(contract_body):
        start = m.start()
        if (depths[start - 1] if start > 0 else 0) != 0:
            continue
        kind = m.group(1) or m.group(3)
        name = m.group(2) or m.group(3)
        parsed = _parse_function(contract_body, content, m.end() - 1, kind, name, start, offset)
        if parsed:
            out.append(parsed)
    return out


def normalize_params(params: str) -> str:
    """Reduce a parameter list to comma-joined types: `uint256 a, bytes calldata b` → `uint256,bytes`.

    Solidity's aliases are expanded (`uint` → `uint256`) so the same function
    written two ways doesn't read as a signature change between versions.
    """
    types: list[str] = []
    for raw in _split_top_level(params):
        tokens = [t for t in raw.split() if t not in _DATA_LOCATIONS]
        if not tokens:
            continue
        # `uint256 amount` → type is the first token; a bare `uint256` keeps it too.
        types.append(_ALIAS_RE.sub(lambda m: _TYPE_ALIASES[m.group(0)], tokens[0]))
    return ",".join(types)


def _declaration_match(cleaned: str, name: str) -> re.Match[str] | None:
    """Match for ``name``'s declaration header; group 1 is the text before its `{`."""
    pattern = re.compile(
        rf"(?:^|[\s;}}])(?:abstract\s+)?(?:{_DECL_KINDS})\s+{re.escape(name)}\b([^{{;]*)\{{",
    )
    return pattern.search(cleaned)


def _find_declaration(cleaned: str, name: str) -> int | None:
    """Index of the `{` opening ``name``'s declaration body, or None."""
    match = _declaration_match(cleaned, name)
    return None if match is None else match.end() - 1


def _match_brace(text: str, open_idx: int) -> int | None:
    """Index of the `}` closing the `{` at ``open_idx``, or None if unbalanced."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _brace_depths(text: str) -> list[int]:
    """Per-character brace depth *after* the character at that index."""
    depths = [0] * len(text)
    depth = 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        depths[i] = depth
    return depths


def _match_paren(text: str, open_idx: int) -> int | None:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _split_top_level(params: str) -> list[str]:
    """Split a parameter list on commas that aren't nested in parens/brackets."""
    parts: list[str] = []
    depth = 0
    current = ""
    for c in params:
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += c
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def _parse_function(
    body: str, content: str, paren_idx: int, kind: str, name: str, start: int, offset: int
) -> FunctionDef | None:
    """Parse one definition starting at its parameter list's `(`.

    Every index is found in ``body`` (masked) and then read from ``content``
    (string literals intact) at the same offsets.
    """
    close = _match_paren(body, paren_idx)
    if close is None:
        return None
    # Parameters carry no string literals — Solidity has no default arguments.
    params = normalize_params(body[paren_idx + 1 : close])

    header_end, terminator = _scan_header(body, close + 1)
    if terminator is None:
        return None
    visibility, modifiers = _parse_header_tokens(body[close + 1 : header_end])
    header_text = content[close + 1 : header_end]

    if terminator == ";":
        return FunctionDef(
            name=name,
            kind=kind,
            params=params,
            visibility=visibility,
            modifiers=modifiers,
            body="",
            has_body=False,
            header=" ".join(header_text.split()),
            span=(start + offset, header_end + 1 + offset),
            literals=_string_literals(header_text),
        )

    body_end = _match_brace(body, header_end)
    if body_end is None:
        return None
    definition_text = content[close + 1 : body_end]
    return FunctionDef(
        name=name,
        kind=kind,
        params=params,
        visibility=visibility,
        modifiers=modifiers,
        body=" ".join(content[header_end + 1 : body_end].split()),
        has_body=True,
        header=" ".join(header_text.split()),
        span=(start + offset, body_end + 1 + offset),
        literals=_string_literals(definition_text),
    )


def _scan_header(body: str, start: int) -> tuple[int, str | None]:
    """Find the `{` or `;` ending the header, ignoring parenthesized clauses."""
    depth = 0
    for i in range(start, len(body)):
        c = body[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and c in "{;":
            return i, c
    return len(body), None


def _parse_header_tokens(header: str) -> tuple[str, tuple[str, ...]]:
    """Split a function header into (visibility, other source-level modifiers).

    `returns (...)` is dropped — it's the signature's tail, not a modifier — as
    are `virtual`/`override`, which say nothing about who may call the function.
    """
    visibility = ""
    modifiers: list[str] = []
    i = 0
    tokens = _tokenize_header(header)
    while i < len(tokens):
        token = tokens[i]
        if token == "returns":
            i += 2 if i + 1 < len(tokens) and tokens[i + 1].startswith("(") else 1
            continue
        if token.startswith("returns("):
            i += 1
            continue
        if token in _VISIBILITIES and not visibility:
            visibility = token
        elif token not in _HEADER_NOISE and not token.startswith("override("):
            modifiers.append(token)
        i += 1
    return visibility, tuple(modifiers)


def _tokenize_header(header: str) -> list[str]:
    """Whitespace tokens, but a parenthesized group stays attached to one token.

    Keeps `onlyRole(ADMIN)` and `returns (uint256)` from splitting into pieces
    that would be read as separate modifiers.
    """
    tokens: list[str] = []
    current = ""
    depth = 0
    for c in header:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c.isspace() and depth == 0:
            if current:
                tokens.append(current)
                current = ""
            continue
        current += c
    if current:
        tokens.append(current)
    return tokens
